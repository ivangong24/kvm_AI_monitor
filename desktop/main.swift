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

enum Panel { case home, usage, settings }

// Shapes matching `kvm_ai_push.py app-usage` — this Mac's local usage snapshot.
struct AppUsage: Decodable {
    let providers: [ProviderPayload]
    let working: [String: Bool]
}

struct ProviderPayload: Decodable {
    let provider: String
    let plan: String?
    let loggedIn: Bool?
    let limits: [LimitPayload]?
    let daily: [DailyPayload]?
    let models: [ModelPayload]?
}

struct ModelPayload: Decodable {
    let model: String
    let tokens: Double
}

struct LimitPayload: Decodable {
    let label: String?
    let usedPercent: Double?
    let windowMinutes: Int?
    let resetsAt: String?
}

struct DailyPayload: Decodable {
    let date: String
    let totalTokens: Double?
    let inputTokens: Double?
    let outputTokens: Double?
    let cacheReadTokens: Double?
    let cacheCreationTokens: Double?
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
    @Published var panel: Panel = .home
    @Published var notice: String?

    @Published var appUsage: AppUsage?
    @Published var usageProvider = "claude"
    @Published var usageLoading = false
    @Published var usageError: String?

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

    private var pushMarker: URL { configDir.appendingPathComponent("last-usage-push") }

    private func readLastPush() -> Date? {
        // The helper writes an ISO timestamp here on each successful push. Prefer its contents;
        // fall back to the file's mtime. (The old /tmp log only changed on errors, so it read stale.)
        if let text = try? String(contentsOf: pushMarker, encoding: .utf8) {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            let formatter = ISO8601DateFormatter()
            formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = formatter.date(from: trimmed) { return date }
            formatter.formatOptions = [.withInternetDateTime]
            if let date = formatter.date(from: trimmed) { return date }
        }
        let attributes = try? FileManager.default.attributesOfItem(atPath: pushMarker.path)
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
        isSending = true
        notice = nil
        // Trigger the installed helper's own LaunchAgent rather than spawning our own push: the
        // agent runs in the context that already has Keychain/network access (a GUI-spawned
        // subprocess may be denied the device secret), so this succeeds where a direct call fails.
        let before = readLastPush()
        let label = "gui/\(getuid())/com.kvm-ai-monitor.helper"
        Task {
            _ = await Task.detached {
                Self.run("/bin/launchctl", ["kickstart", "-k", label])
            }.value
            // The push runs asynchronously; watch the success marker to confirm it landed.
            var updated = false
            for _ in 0..<24 {
                try? await Task.sleep(nanoseconds: 500_000_000)
                if let now = readLastPush(), before == nil || now > before! { updated = true; break }
            }
            isSending = false
            notice = updated
                ? "Usage updated on your Comet Pro."
                : "Still updating — this can take a moment. Open the dashboard if it persists."
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

    func loadUsage(force: Bool = false) {
        guard helperInstalled, !usageLoading else {
            if !helperInstalled { usageError = "Add this Mac first to see its AI usage." }
            return
        }
        if appUsage != nil && !force { return }
        let script = helperScript.path
        usageLoading = true
        usageError = nil
        Task {
            let result = await Task.detached {
                Self.run("/usr/bin/env", ["python3", script, "app-usage"])
            }.value
            usageLoading = false
            // stdout may carry stderr "# provider: error" notes; the payload is the JSON line.
            let jsonLine = result.output
                .split(separator: "\n")
                .last { $0.trimmingCharacters(in: .whitespaces).hasPrefix("{") }
            if let line = jsonLine, let data = String(line).data(using: .utf8),
               let parsed = try? JSONDecoder().decode(AppUsage.self, from: data) {
                appUsage = parsed
                if !parsed.providers.contains(where: { $0.provider == usageProvider }),
                   let first = parsed.providers.first {
                    usageProvider = first.provider
                }
                usageError = parsed.providers.isEmpty ? "No usage yet — sign in to Claude or Codex on this Mac." : nil
            } else {
                usageError = "Couldn’t read usage from this Mac."
            }
        }
    }

    static func providerName(_ id: String) -> String {
        ["claude": "Claude Code", "codex": "Codex", "copilot": "GitHub Copilot",
         "gemini": "Gemini CLI", "grok": "Grok Build"][id] ?? id.capitalized
    }
}

// A small template glyph echoing the Comet Pro: a boxy device with a screen. Filled screen means
// everything is live; a hollow screen means setup is still needed. Template art adapts to the
// menu bar's light/dark tint automatically.
func statusBarIcon(healthy: Bool) -> NSImage {
    let size = NSSize(width: 18, height: 14)
    let image = NSImage(size: size)
    image.lockFocus()
    NSColor.black.setStroke()
    NSColor.black.setFill()

    let body = NSBezierPath(roundedRect: NSRect(x: 1.6, y: 1.7, width: 14.8, height: 10.6), xRadius: 2.6, yRadius: 2.6)
    body.lineWidth = 1.4
    body.stroke()

    // Filled screen means everything is live; a hollow screen means setup is still needed.
    let screen = NSBezierPath(roundedRect: NSRect(x: 4.2, y: 4.6, width: 9.6, height: 4.8), xRadius: 1.2, yRadius: 1.2)
    if healthy {
        screen.fill()
    } else {
        screen.lineWidth = 1.2
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
                switch model.panel {
                case .home: homeBody
                case .usage: UsagePanel(model: model)
                case .settings: settingsBody
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
                Text(headerSubtitle)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.white.opacity(0.67))
            }
            Spacer()
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

    private var headerSubtitle: String {
        switch model.panel {
        case .home: return "AI usage on your Comet Pro"
        case .usage: return "This Mac’s AI usage"
        case .settings: return "Settings"
        }
    }

    private func navButton(_ target: Panel, _ title: String, _ icon: String) -> some View {
        Button {
            model.panel = target
            if target == .usage { model.loadUsage() }
        } label: {
            Label(title, systemImage: icon)
                .labelStyle(.titleAndIcon)
                .foregroundStyle(model.panel == target ? accent : Color.secondary)
        }
        .buttonStyle(.plain)
    }

    private var footer: some View {
        HStack(spacing: 14) {
            navButton(.home, "Home", "house")
            navButton(.usage, "Usage", "chart.bar")
            navButton(.settings, "Settings", "gearshape")
            Spacer()
            Button { model.refresh(); if model.panel == .usage { model.loadUsage(force: true) } } label: { Image(systemName: "arrow.clockwise") }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Refresh")
            Button { NSApplication.shared.terminate(nil) } label: { Image(systemName: "power") }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Quit KVM AI Monitor")
        }
        .font(.system(size: 11, weight: .medium))
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
        .background(Color.primary.opacity(0.035))
        .overlay(alignment: .top) { Divider() }
    }
}

// MARK: - Usage pane

func tokenShort(_ value: Double) -> String {
    if value >= 1e9 { return String(format: value >= 1e10 ? "%.0fB" : "%.1fB", value / 1e9) }
    if value >= 1e6 { return String(format: value >= 1e7 ? "%.0fM" : "%.1fM", value / 1e6) }
    if value >= 1e3 { return "\(Int((value / 1e3).rounded()))K" }
    return "\(Int(value.rounded()))"
}

// "claude-opus-4-8" -> "Opus 4.8", "claude-3-5-haiku-20241022" -> "Haiku 3.5", "gpt-5.6" -> "GPT 5.6".
func prettyModel(_ raw: String) -> String {
    let lower = raw.lowercased()
    let families: [(String, String)] = [
        ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"),
        ("gpt", "GPT"), ("gemini", "Gemini"), ("grok", "Grok"),
    ]
    var family: String?
    for (key, name) in families where lower.contains(key) { family = name; break }
    guard let name = family else { return raw.count > 18 ? String(raw.prefix(18)) : raw }
    let groups = raw.components(separatedBy: CharacterSet(charactersIn: "-.")).filter { !$0.isEmpty && $0.allSatisfy(\.isNumber) }
    var parts: [String] = []
    for group in groups {
        if group.count <= 2 { parts.append(group) } else { break }
        if parts.count == 2 { break }
    }
    return parts.isEmpty ? name : "\(name) \(parts.joined(separator: "."))"
}

func resetRelative(_ iso: String?) -> String? {
    guard let iso else { return nil }
    let formatter = ISO8601DateFormatter()
    formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    var date = formatter.date(from: iso)
    if date == nil { formatter.formatOptions = [.withInternetDateTime]; date = formatter.date(from: iso) }
    guard let date else { return nil }
    let seconds = date.timeIntervalSinceNow
    if seconds <= 0 { return "resets now" }
    let minutes = Int(seconds / 60)
    if minutes >= 2880 { return "resets in \(Int((Double(minutes) / 1440).rounded()))d" }
    if minutes >= 120 { return "resets in \(Int((Double(minutes) / 60).rounded()))h" }
    return "resets in \(minutes)m"
}

private struct UsageLimitBar: View {
    let title: String
    let limit: LimitPayload
    let tint: Color

    var body: some View {
        let percent = limit.usedPercent ?? 0
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(title.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(0.4).foregroundStyle(.secondary)
                Spacer()
                Text(limit.usedPercent == nil ? "--" : "\(Int(percent))%").font(.system(size: 12, weight: .semibold))
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.09)).frame(height: 8)
                    Capsule().fill(tint).frame(width: max(8, geo.size.width * CGFloat(min(100, percent)) / 100), height: 8)
                }
            }
            .frame(height: 8)
            if let reset = resetRelative(limit.resetsAt) {
                Text(reset).font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
    }
}

private struct DailyBars: View {
    let values: [Double]
    let tint: Color

    var body: some View {
        let maxValue = max(values.max() ?? 1, 1)
        VStack(alignment: .leading, spacing: 6) {
            GeometryReader { geo in
                HStack(alignment: .bottom, spacing: 3) {
                    ForEach(Array(values.enumerated()), id: \.offset) { _, value in
                        RoundedRectangle(cornerRadius: 2)
                            .fill(tint.opacity(0.85))
                            .frame(maxWidth: .infinity)
                            .frame(height: max(2, geo.size.height * CGFloat(value / maxValue)))
                    }
                }
            }
            .frame(height: 70)
            HStack {
                Text("peak \(tokenShort(maxValue))").font(.system(size: 10)).foregroundStyle(.secondary)
                Spacer()
                Text("today \(tokenShort(values.last ?? 0))").font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
    }
}

private struct DonutSlice: Identifiable {
    let id = UUID()
    let label: String
    let value: Double
    let color: Color
}

private struct DonutChart: View {
    let slices: [DonutSlice]

    private var total: Double { max(slices.reduce(0) { $0 + $1.value }, 1) }

    private struct Arc: Identifiable { let id = UUID(); let start: CGFloat; let end: CGFloat; let color: Color }

    private var arcs: [Arc] {
        var accumulated: CGFloat = 0
        var result: [Arc] = []
        for slice in slices {
            let fraction = CGFloat(slice.value / total)
            result.append(Arc(start: accumulated, end: accumulated + fraction, color: slice.color))
            accumulated += fraction
        }
        return result
    }

    var body: some View {
        HStack(spacing: 16) {
            ZStack {
                ForEach(arcs) { arc in
                    Circle()
                        .trim(from: arc.start, to: arc.end)
                        .stroke(arc.color, style: StrokeStyle(lineWidth: 13, lineCap: .butt))
                        .rotationEffect(.degrees(-90))
                }
            }
            .frame(width: 78, height: 78)
            VStack(alignment: .leading, spacing: 6) {
                ForEach(slices) { slice in
                    HStack(spacing: 7) {
                        Circle().fill(slice.color).frame(width: 8, height: 8)
                        Text(slice.label).font(.system(size: 11)).foregroundStyle(.secondary)
                        Spacer()
                        Text("\(Int((slice.value / total * 100).rounded()))%").font(.system(size: 11, weight: .semibold))
                    }
                }
            }
        }
    }
}

struct UsagePanel: View {
    @ObservedObject var model: MonitorModel
    private let accent = Color(red: 0.22, green: 0.49, blue: 0.96)

    private var providers: [ProviderPayload] { model.appUsage?.providers ?? [] }
    private var selected: ProviderPayload? {
        providers.first { $0.provider == model.usageProvider } ?? providers.first
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            if !model.helperInstalled {
                infoCard("Set up this Mac", "Enroll this Mac from the Home tab to see its AI usage here.")
            } else if let provider = selected {
                if providers.count > 1 { providerPicker }
                workingCard(provider)
                limitsCard(provider)
                dailyCard(provider)
                compositionCard(provider)
            } else if model.usageLoading {
                loadingView
            } else {
                infoCard("No usage yet", model.usageError ?? "Sign in to Claude or Codex on this Mac.")
            }
        }
        .padding(16)
        .onAppear { model.loadUsage() }
    }

    private var providerPicker: some View {
        Picker("", selection: $model.usageProvider) {
            ForEach(providers, id: \.provider) { provider in
                Text(MonitorModel.providerName(provider.provider)).tag(provider.provider)
            }
        }
        .pickerStyle(.segmented)
        .labelsHidden()
    }

    private func workingCard(_ provider: ProviderPayload) -> some View {
        let working = model.appUsage?.working[provider.provider] ?? false
        let color = working ? Color.green : Color.secondary
        return HStack {
            VStack(alignment: .leading, spacing: 3) {
                Text(MonitorModel.providerName(provider.provider)).font(.system(size: 14, weight: .semibold))
                Text(provider.plan.map { $0.uppercased() } ?? "—")
                    .font(.system(size: 11, weight: .semibold)).foregroundStyle(accent)
            }
            Spacer()
            HStack(spacing: 6) {
                Circle().fill(color).frame(width: 7, height: 7)
                Text(working ? "WORKING" : "READY").font(.system(size: 10, weight: .bold)).tracking(0.6)
            }
            .foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(color.opacity(0.14), in: Capsule())
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
    }

    private func limitsCard(_ provider: ProviderPayload) -> some View {
        card("Limits") {
            if let limits = provider.limits, !limits.isEmpty {
                VStack(spacing: 12) {
                    ForEach(Array(limits.prefix(3).enumerated()), id: \.offset) { _, limit in
                        UsageLimitBar(title: limit.label ?? "Limit", limit: limit, tint: accent)
                    }
                }
            } else {
                Text("No limit data available.").font(.system(size: 12)).foregroundStyle(.secondary)
            }
        }
    }

    private func dailyCard(_ provider: ProviderPayload) -> some View {
        let values = Array((provider.daily ?? []).suffix(14)).map { $0.totalTokens ?? 0 }
        return card("Daily tokens · last \(values.count) days") {
            if values.isEmpty {
                Text("No daily history yet.").font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                DailyBars(values: values, tint: accent)
            }
        }
    }

    private func compositionCard(_ provider: ProviderPayload) -> some View {
        let palette: [Color] = [
            accent, Color(red: 0.28, green: 0.7, blue: 0.55), Color(red: 0.85, green: 0.6, blue: 0.25),
            Color(red: 0.6, green: 0.45, blue: 0.85), Color(red: 0.9, green: 0.42, blue: 0.42), Color.gray,
        ]
        let models = (provider.models ?? []).filter { $0.tokens > 0 }
        var slices: [DonutSlice] = models.prefix(5).enumerated().map { index, model in
            DonutSlice(label: prettyModel(model.model), value: model.tokens, color: palette[index % palette.count])
        }
        let remainder = models.dropFirst(5).reduce(0) { $0 + $1.tokens }
        if remainder > 0 { slices.append(DonutSlice(label: "Other", value: remainder, color: palette[5])) }
        return card("Models · 30 days") {
            if slices.isEmpty {
                Text("Per-model usage isn’t reported by \(MonitorModel.providerName(provider.provider)).")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                DonutChart(slices: slices)
            }
        }
    }

    private func card<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title.uppercased()).font(.system(size: 9, weight: .bold)).tracking(0.9).foregroundStyle(.secondary)
            content()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 13))
        .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
    }

    private func infoCard(_ title: String, _ detail: String) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.system(size: 13, weight: .semibold))
            Text(detail).font(.system(size: 11)).foregroundStyle(.secondary)
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(accent.opacity(0.06), in: RoundedRectangle(cornerRadius: 13))
    }

    private var loadingView: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text("Reading usage from this Mac…").font(.system(size: 12)).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity).padding(24)
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
