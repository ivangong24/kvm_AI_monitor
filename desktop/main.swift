// KVM AI Monitor menu bar companion.
//
// A compact native control surface for pairing this Mac with a Comet Pro and keeping its AI-usage
// screen up to date. It reads the same local state as the CLI/helper and never stores KVM
// credentials of its own. Wording favours plain language over the internal "helper/push" terms.

import AppKit
import ServiceManagement
import SwiftUI

struct PushTarget: Identifiable, Hashable {
    var id: String { host + deviceId }
    let host: String
    let deviceId: String
}

@MainActor
final class MonitorModel: ObservableObject {
    @Published var kvms: [String] = []
    @Published var targets: [PushTarget] = []
    @Published var helperLoaded = false
    @Published var lastPushDate: Date?
    @Published var isSending = false
    @Published var launchAtLogin = false
    @Published var hooksEnabled = false
    @Published var isTogglingHooks = false
    @Published var showSettings = false
    @Published var notice: String?

    private var configDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".kvm-ai-monitor")
    }

    private var helperScript: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/kvm-ai-monitor/kvm_ai_push.py")
    }

    private var claudeSettings: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".claude/settings.json")
    }

    var helperInstalled: Bool { FileManager.default.fileExists(atPath: helperScript.path) }

    var overallTitle: String {
        if kvms.isEmpty { return "Ready to connect" }
        if helperLoaded && !targets.isEmpty { return "You’re all set" }
        return "Almost there"
    }

    var overallDetail: String {
        if kvms.isEmpty { return "Pair a Comet Pro to show your AI usage on its screen." }
        if targets.isEmpty { return "Your Comet Pro is set up — add this Mac so its AI usage shows on the screen." }
        if !helperLoaded { return "This Mac is added, but automatic updates aren’t running yet." }
        let noun = targets.count == 1 ? "Comet Pro" : "Comet Pros"
        return "This Mac’s AI usage is updating live on \(targets.count) \(noun)."
    }

    var isHealthy: Bool { !kvms.isEmpty && !targets.isEmpty && helperLoaded }

    var lastPushText: String {
        guard let date = lastPushDate else { return "Not yet" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: date, relativeTo: Date())
    }

    func refresh() {
        kvms = readKvms()
        targets = readTargets()
        helperLoaded = launchAgentLoaded()
        lastPushDate = readLastPush()
        launchAtLogin = SMAppService.mainApp.status == .enabled
        hooksEnabled = readHooksEnabled()
    }

    private func readJSON(_ url: URL) -> [String: Any]? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    private func readKvms() -> [String] {
        if let parsed = readJSON(configDir.appendingPathComponent("kvms.json")),
           let hosts = parsed["kvms"] as? [String], !hosts.isEmpty {
            return hosts
        }
        if let single = try? String(
            contentsOf: configDir.appendingPathComponent("kvm-host"), encoding: .utf8
        ).trimmingCharacters(in: .whitespacesAndNewlines), !single.isEmpty {
            return [single]
        }
        return []
    }

    private func readTargets() -> [PushTarget] {
        guard let parsed = readJSON(configDir.appendingPathComponent("helper.json")) else { return [] }
        if let raw = parsed["targets"] as? [[String: Any]] {
            return raw.compactMap { entry in
                guard let host = entry["kvmHost"] as? String,
                      let device = entry["deviceId"] as? String,
                      !host.isEmpty, !device.isEmpty else { return nil }
                return PushTarget(host: host, deviceId: device)
            }
        }
        if let host = parsed["kvmHost"] as? String, let device = parsed["deviceId"] as? String {
            return [PushTarget(host: host, deviceId: device)]
        }
        return []
    }

    private func launchAgentLoaded() -> Bool {
        Self.run("/bin/launchctl", ["print", "gui/\(getuid())/com.kvm-ai-monitor.helper"]).status == 0
    }

    private func readLastPush() -> Date? {
        let log = URL(fileURLWithPath: "/tmp/kvm-ai-helper.log")
        let attributes = try? FileManager.default.attributesOfItem(atPath: log.path)
        return attributes?[.modificationDate] as? Date
    }

    private func readHooksEnabled() -> Bool {
        guard let text = try? String(contentsOf: claudeSettings, encoding: .utf8) else { return false }
        return text.contains("kvm-ai-claude-hook")
    }

    nonisolated private static func run(
        _ tool: String, _ arguments: [String]
    ) -> (status: Int32, output: String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
        } catch {
            return (1, error.localizedDescription)
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    func openAIUsage(host: String) {
        guard let url = URL(string: "https://\(host)/extras/ai-usage/") else { return }
        NSWorkspace.shared.open(url)
    }

    func openDashboard() {
        if let host = kvms.first { openAIUsage(host: host) }
    }

    func sendUsageNow() {
        guard !targets.isEmpty, !isSending else { return }
        let script = helperScript.path
        isSending = true
        notice = nil
        Task {
            let result = await Task.detached {
                Self.run("/usr/bin/env", ["python3", script, "send-usage"])
            }.value
            isSending = false
            notice = result.status == 0 ? "Usage updated on your Comet Pro." : "Update failed — open the dashboard for details."
            refresh()
        }
    }

    func runSetupInTerminal() {
        let command = "command -v kvm-ai-monitor >/dev/null 2>&1 || brew install ivangong24/kvm-ai-monitor/kvm-ai-monitor; kvm-ai-monitor"
        let source = """
        tell application "Terminal"
            activate
            do script "\(command)"
        end tell
        """
        NSAppleScript(source: source)?.executeAndReturnError(nil)
    }

    func toggleLaunchAtLogin() {
        do {
            if SMAppService.mainApp.status == .enabled {
                try SMAppService.mainApp.unregister()
            } else {
                try SMAppService.mainApp.register()
            }
            launchAtLogin = SMAppService.mainApp.status == .enabled
        } catch {
            notice = "Could not change Login Items: \(error.localizedDescription)"
        }
    }

    func toggleHooks() {
        guard helperInstalled, !isTogglingHooks else {
            if !helperInstalled { notice = "Add this Mac first, then you can turn on precise Claude activity." }
            return
        }
        let script = helperScript.path
        let action = hooksEnabled ? "uninstall-hooks" : "install-hooks"
        isTogglingHooks = true
        notice = nil
        Task {
            let result = await Task.detached {
                Self.run("/usr/bin/env", ["python3", script, action])
            }.value
            isTogglingHooks = false
            if result.status != 0 { notice = "Could not change the Claude activity setting." }
            refresh()
        }
    }
}

// A small template glyph echoing the Comet Pro: a boxy device with a screen. Filled screen means
// everything is live; a hollow screen means setup is still needed. Template art adapts to the
// menu bar's light/dark tint automatically.
func statusBarIcon(healthy: Bool) -> NSImage {
    let size = NSSize(width: 19, height: 15)
    let image = NSImage(size: size)
    image.lockFocus()
    NSColor.black.setStroke()
    NSColor.black.setFill()

    // Depth hint: a short top-back edge for the 3/4 look.
    let topEdge = NSBezierPath()
    topEdge.move(to: NSPoint(x: 4.5, y: 12.4))
    topEdge.line(to: NSPoint(x: 7.5, y: 13.6))
    topEdge.line(to: NSPoint(x: 16.5, y: 13.6))
    topEdge.lineWidth = 1.2
    topEdge.lineCapStyle = .round
    topEdge.lineJoinStyle = .round
    topEdge.stroke()

    let body = NSBezierPath(roundedRect: NSRect(x: 2, y: 1.6, width: 13, height: 10.8), xRadius: 2.2, yRadius: 2.2)
    body.lineWidth = 1.3
    body.stroke()

    let screen = NSBezierPath(roundedRect: NSRect(x: 4.1, y: 5.2, width: 8.8, height: 4.6), xRadius: 1, yRadius: 1)
    if healthy {
        screen.fill()
    } else {
        screen.lineWidth = 1.1
        screen.stroke()
    }
    image.unlockFocus()
    image.isTemplate = true
    return image
}

private struct BrandMark: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .fill(.white.opacity(0.14))
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .stroke(.white.opacity(0.22), lineWidth: 1)
            // A boxy Comet Pro silhouette with a lit screen.
            ZStack {
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .stroke(.white.opacity(0.85), lineWidth: 2)
                    .frame(width: 26, height: 20)
                RoundedRectangle(cornerRadius: 2, style: .continuous)
                    .fill(.white)
                    .frame(width: 16, height: 7)
                    .offset(y: -1)
            }
        }
        .frame(width: 44, height: 44)
    }
}

private struct StatusOrb: View {
    let healthy: Bool

    var body: some View {
        Circle()
            .fill(healthy ? Color.green : Color.orange)
            .frame(width: 8, height: 8)
            .overlay(Circle().stroke(.white.opacity(0.8), lineWidth: 1))
            .shadow(color: (healthy ? Color.green : Color.orange).opacity(0.5), radius: 4)
    }
}

private struct MetricCard: View {
    let icon: String
    let title: String
    let value: String
    let caption: String
    let tint: Color

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Image(systemName: icon)
                .font(.system(size: 12, weight: .semibold))
                .foregroundStyle(tint)
                .frame(width: 24, height: 24)
                .background(tint.opacity(0.12), in: RoundedRectangle(cornerRadius: 7))
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold))
                .tracking(0.8)
                .foregroundStyle(.secondary)
            Text(value)
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(1)
            Text(caption)
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.045), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
    }
}

private struct KVMRow: View {
    let host: String
    let enrolled: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: 12) {
                ZStack {
                    RoundedRectangle(cornerRadius: 9, style: .continuous)
                        .fill(LinearGradient(
                            colors: [Color(red: 0.14, green: 0.18, blue: 0.25), .black],
                            startPoint: .topLeading, endPoint: .bottomTrailing
                        ))
                    RoundedRectangle(cornerRadius: 3)
                        .fill(Color.blue.opacity(0.7))
                        .frame(width: 23, height: 9)
                    Circle().fill(.green).frame(width: 3, height: 3).offset(x: 15, y: 10)
                }
                .frame(width: 48, height: 34)
                VStack(alignment: .leading, spacing: 3) {
                    Text("Comet Pro")
                        .font(.system(size: 13, weight: .semibold))
                    HStack(spacing: 5) {
                        StatusOrb(healthy: enrolled)
                        Text(host)
                            .font(.system(size: 11, design: .monospaced))
                            .foregroundStyle(.secondary)
                    }
                }
                Spacer()
                Text("Open screen")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(.secondary)
                Image(systemName: "arrow.up.right")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(12)
        .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
    }
}

private struct PrimaryActionStyle: ButtonStyle {
    let tint: Color

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 12, weight: .semibold))
            .foregroundStyle(.white)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 10)
            .background(tint.opacity(configuration.isPressed ? 0.76 : 1), in: RoundedRectangle(cornerRadius: 10))
            .scaleEffect(configuration.isPressed ? 0.985 : 1)
    }
}

private struct SettingRow<Control: View>: View {
    let title: String
    let detail: String
    @ViewBuilder let control: () -> Control

    var body: some View {
        HStack(alignment: .center, spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            control()
        }
        .padding(.vertical, 10)
    }
}

struct CompanionPanel: View {
    @ObservedObject var model: MonitorModel

    private let accent = Color(red: 0.22, green: 0.49, blue: 0.96)

    var body: some View {
        VStack(spacing: 0) {
            header
            ScrollView {
                if model.showSettings {
                    settingsBody
                } else {
                    homeBody
                }
            }
            footer
        }
        .frame(width: 382, height: 560)
        .background(.regularMaterial)
        .onAppear { model.refresh() }
    }

    // MARK: Header

    private var header: some View {
        HStack(spacing: 12) {
            BrandMark()
            VStack(alignment: .leading, spacing: 2) {
                Text("KVM AI Monitor")
                    .font(.system(size: 16, weight: .bold, design: .rounded))
                    .foregroundStyle(.white)
                Text(model.showSettings ? "Settings" : "AI usage on your Comet Pro")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.67))
            }
            Spacer()
            if model.showSettings {
                Button { model.showSettings = false } label: {
                    Label("Done", systemImage: "chevron.left")
                        .labelStyle(.titleOnly)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 6)
                        .background(.white.opacity(0.14), in: Capsule())
                }
                .buttonStyle(.plain)
            } else {
                HStack(spacing: 6) {
                    StatusOrb(healthy: model.isHealthy)
                    Text(model.isHealthy ? "LIVE" : "CHECK")
                        .font(.system(size: 9, weight: .bold))
                        .tracking(0.8)
                }
                .foregroundStyle(.white.opacity(0.9))
                .padding(.horizontal, 9)
                .padding(.vertical, 6)
                .background(.white.opacity(0.11), in: Capsule())
            }
        }
        .padding(16)
        .background(
            LinearGradient(
                colors: [Color(red: 0.08, green: 0.15, blue: 0.29), Color(red: 0.13, green: 0.32, blue: 0.65)],
                startPoint: .topLeading, endPoint: .bottomTrailing
            )
        )
    }

    // MARK: Home

    private var homeBody: some View {
        VStack(alignment: .leading, spacing: 14) {
            statusIntro
            if model.kvms.isEmpty { emptyState } else { kvmList }
            metrics
            actions
            if let notice = model.notice {
                Label(notice, systemImage: "info.circle.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 2)
            }
        }
        .padding(16)
    }

    private var statusIntro: some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(model.overallTitle)
                .font(.system(size: 19, weight: .bold, design: .rounded))
            Text(model.overallDetail)
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
    }

    private var emptyState: some View {
        HStack(spacing: 13) {
            Image(systemName: "display.trianglebadge.exclamationmark")
                .font(.system(size: 22, weight: .medium))
                .foregroundStyle(accent)
                .frame(width: 42, height: 42)
                .background(accent.opacity(0.11), in: RoundedRectangle(cornerRadius: 12))
            VStack(alignment: .leading, spacing: 3) {
                Text("No Comet paired yet")
                    .font(.system(size: 13, weight: .semibold))
                Text("The guided setup finds it on your local network.")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            }
        }
        .padding(14)
        .background(accent.opacity(0.055), in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(accent.opacity(0.16)))
    }

    private var kvmList: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("YOUR COMET PRO")
                .font(.system(size: 9, weight: .bold))
                .tracking(0.9)
                .foregroundStyle(.secondary)
            ForEach(model.kvms, id: \.self) { host in
                KVMRow(host: host, enrolled: model.targets.contains { $0.host == host }) {
                    model.openAIUsage(host: host)
                }
            }
        }
    }

    private var metrics: some View {
        HStack(spacing: 10) {
            MetricCard(
                icon: model.helperLoaded ? "checkmark.circle.fill" : "pause.circle.fill",
                title: "Auto-sync",
                value: model.helperLoaded ? "On" : "Paused",
                caption: model.helperLoaded ? "Updates on a schedule" : "Not running yet",
                tint: model.helperLoaded ? .green : .orange
            )
            MetricCard(
                icon: "clock.arrow.circlepath",
                title: "Last update",
                value: model.lastPushText,
                caption: "How fresh the screen is",
                tint: accent
            )
        }
    }

    private var actions: some View {
        HStack(spacing: 10) {
            Button { model.sendUsageNow() } label: {
                Label(model.isSending ? "Updating…" : "Update now", systemImage: "arrow.clockwise")
            }
            .buttonStyle(PrimaryActionStyle(tint: accent))
            .disabled(model.targets.isEmpty || model.isSending)

            Button { model.runSetupInTerminal() } label: {
                Label(model.kvms.isEmpty ? "Set up" : "Add or fix a device", systemImage: "plus.circle.fill")
            }
            .buttonStyle(PrimaryActionStyle(tint: Color(red: 0.24, green: 0.27, blue: 0.33)))
        }
    }

    // MARK: Settings

    private var settingsBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            settingsGroup(title: "General") {
                SettingRow(title: "Open at login",
                           detail: "Keep the Comet Pro screen updating whenever your Mac is on.") {
                    Toggle("", isOn: Binding(get: { model.launchAtLogin }, set: { _ in model.toggleLaunchAtLogin() }))
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
            }

            settingsGroup(title: "Claude activity") {
                SettingRow(title: "Precise “working” animation",
                           detail: model.helperInstalled
                                ? "Optional. Shows Claude’s working animation the instant it starts, by adding hooks to ~/.claude/settings.json. Usage tracking works either way."
                                : "Add this Mac first to enable. Usage tracking never needs this.") {
                    if model.isTogglingHooks {
                        ProgressView().controlSize(.small)
                    } else {
                        Toggle("", isOn: Binding(get: { model.hooksEnabled }, set: { _ in model.toggleHooks() }))
                            .labelsHidden()
                            .toggleStyle(.switch)
                            .disabled(!model.helperInstalled)
                    }
                }
            }

            settingsGroup(title: "Shortcuts") {
                Button { model.openDashboard() } label: {
                    settingsLink(icon: "safari", title: "Open AI Usage dashboard",
                                 detail: "Providers, appearance, and the live screen.")
                }
                .buttonStyle(.plain)
                .disabled(model.kvms.isEmpty)
                Divider()
                Button { model.runSetupInTerminal() } label: {
                    settingsLink(icon: "terminal", title: "Run setup / manage devices",
                                 detail: "Pair a Comet Pro or enroll another Mac.")
                }
                .buttonStyle(.plain)
            }

            if let notice = model.notice {
                Label(notice, systemImage: "info.circle.fill")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 2)
                    .padding(.top, 2)
            }

            Text("KVM AI Monitor for GL.iNet Comet Pro")
                .font(.system(size: 10.5))
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.top, 6)
        }
        .padding(16)
    }

    private func settingsGroup<Content: View>(title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title.uppercased())
                .font(.system(size: 9, weight: .bold))
                .tracking(0.9)
                .foregroundStyle(.secondary)
                .padding(.bottom, 2)
            VStack(alignment: .leading, spacing: 0) { content() }
                .padding(.horizontal, 14)
                .padding(.vertical, 2)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 13, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
        }
        .padding(.bottom, 10)
    }

    private func settingsLink(icon: String, title: String, detail: String) -> some View {
        HStack(spacing: 12) {
            Image(systemName: icon)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(accent)
                .frame(width: 26)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.system(size: 13, weight: .semibold))
                Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            Image(systemName: "chevron.right").font(.system(size: 11, weight: .semibold)).foregroundStyle(.secondary)
        }
        .contentShape(Rectangle())
        .padding(.vertical, 10)
    }

    // MARK: Footer

    private var footer: some View {
        HStack(spacing: 14) {
            Button { model.showSettings.toggle() } label: {
                Label(model.showSettings ? "Home" : "Settings",
                      systemImage: model.showSettings ? "house" : "gearshape")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.secondary)
            Spacer()
            Button { model.refresh() } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.plain)
                .help("Refresh status")
            Button { NSApplication.shared.terminate(nil) } label: { Image(systemName: "power") }
                .buttonStyle(.plain)
                .help("Quit KVM AI Monitor")
        }
        .font(.system(size: 11, weight: .medium))
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.primary.opacity(0.035))
        .overlay(alignment: .top) { Divider() }
    }
}

#if PREVIEW_APP
@main
struct KVMAIMonitorPreviewApp: App {
    @StateObject private var model = MonitorModel()

    var body: some Scene {
        WindowGroup("KVM AI Monitor Preview") {
            CompanionPanel(model: model)
        }
        .windowResizability(.contentSize)
    }
}
#else
@main
struct KVMAIMonitorApp: App {
    @StateObject private var model = MonitorModel()

    var body: some Scene {
        MenuBarExtra {
            CompanionPanel(model: model)
        } label: {
            Image(nsImage: statusBarIcon(healthy: model.isHealthy))
        }
        .menuBarExtraStyle(.window)
    }
}
#endif
