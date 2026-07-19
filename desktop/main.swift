// KVM AI Monitor menu bar companion.
//
// Shows enrollment/push health for this Mac and offers one-click actions: open the AI Usage
// page, push usage now, or run the guided setup wizard in Terminal. All state comes from the
// same files the CLI and helper use (~/.kvm-ai-monitor, the LaunchAgent, /tmp/kvm-ai-helper.log),
// so the app never stores credentials of its own.

import AppKit
import SwiftUI

struct PushTarget: Identifiable {
    var id: String { host + deviceId }
    let host: String
    let deviceId: String
}

@MainActor
final class MonitorModel: ObservableObject {
    @Published var kvms: [String] = []
    @Published var targets: [PushTarget] = []
    @Published var helperLoaded = false
    @Published var lastPush = "never"

    private var configDir: URL {
        FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".kvm-ai-monitor")
    }

    private var helperScript: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/kvm-ai-monitor/kvm_ai_push.py")
    }

    func refresh() {
        kvms = readKvms()
        targets = readTargets()
        helperLoaded = launchAgentLoaded()
        lastPush = readLastPush()
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
        if let single = try? String(contentsOf: configDir.appendingPathComponent("kvm-host"), encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), !single.isEmpty {
            return [single]
        }
        return []
    }

    private func readTargets() -> [PushTarget] {
        guard let parsed = readJSON(configDir.appendingPathComponent("helper.json")) else { return [] }
        if let raw = parsed["targets"] as? [[String: Any]] {
            return raw.compactMap { entry in
                guard let host = entry["kvmHost"] as? String, let device = entry["deviceId"] as? String,
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
        let result = run("/bin/launchctl", ["print", "gui/\(getuid())/com.kvm-ai-monitor.helper"])
        return result.status == 0
    }

    private func readLastPush() -> String {
        let log = URL(fileURLWithPath: "/tmp/kvm-ai-helper.log")
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: log.path),
              let modified = attributes[.modificationDate] as? Date else { return "never" }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .abbreviated
        return formatter.localizedString(for: modified, relativeTo: Date())
    }

    @discardableResult
    private func run(_ tool: String, _ arguments: [String]) -> (status: Int32, output: String) {
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
        if let url = URL(string: "https://\(host)/extras/ai-usage/") {
            NSWorkspace.shared.open(url)
        }
    }

    func sendUsageNow() {
        let script = helperScript.path
        Task.detached { [weak self] in
            _ = await self?.run("/usr/bin/env", ["python3", script, "send-usage"])
            await self?.refresh()
        }
    }

    func runSetupInTerminal() {
        let command = "npx github:ivangong24/kvm_AI_monitor"
        let source = """
        tell application "Terminal"
            activate
            do script "\(command)"
        end tell
        """
        if let apple = NSAppleScript(source: source) {
            apple.executeAndReturnError(nil)
        }
    }
}

struct MenuContent: View {
    @ObservedObject var model: MonitorModel

    var body: some View {
        if model.kvms.isEmpty {
            Text("No KVM configured yet")
        } else {
            ForEach(model.kvms, id: \.self) { host in
                Button("Open AI Usage — \(host)") { model.openAIUsage(host: host) }
            }
        }
        Divider()
        Text(model.targets.isEmpty
             ? "This Mac is not enrolled"
             : "Enrolled: \(model.targets.map(\.host).joined(separator: ", "))")
        Text("Helper: \(model.helperLoaded ? "scheduled" : "not loaded") · last push \(model.lastPush)")
        Divider()
        Button("Send usage now") { model.sendUsageNow() }
            .disabled(model.targets.isEmpty)
        Button("Run setup wizard in Terminal…") { model.runSetupInTerminal() }
        Button("Refresh status") { model.refresh() }
        Divider()
        Button("Quit") { NSApplication.shared.terminate(nil) }
    }
}

@main
struct KVMAIMonitorApp: App {
    @StateObject private var model = MonitorModel()

    var body: some Scene {
        MenuBarExtra("KVM AI Monitor", systemImage: "sparkles.tv") {
            MenuContent(model: model)
                .onAppear { model.refresh() }
        }
    }
}
