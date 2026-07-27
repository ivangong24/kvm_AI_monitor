// KVM AI Monitor menu bar companion.
//
// A compact native control surface for pairing this Mac with a Comet Pro and keeping its AI-usage
// screen up to date. It reads the same local state as the CLI/helper and never stores KVM
// credentials of its own. Wording favours plain language over the internal "helper/push" terms.

import AppKit
import Combine
import ServiceManagement
import SwiftUI
import UserNotifications

struct PushTarget: Identifiable, Hashable {
    var id: String { host + deviceId }
    let host: String
    let deviceId: String
}

enum Panel { case home, usage, settings }

// What the macOS menu bar shows at a glance — mirrors CodexBar/ClaudeBar's live metric.
enum MenuBarMode: String, CaseIterable, Identifiable {
    case icon, gauge, limitPercent, costToday, dual
    var id: String { rawValue }
    var label: String {
        switch self {
        case .icon: return "Icon only"
        case .gauge: return "Usage gauge"
        case .limitPercent: return "Limit %"
        case .costToday: return "Cost today"
        case .dual: return "Session + weekly"
        }
    }
}

// Preset looks for the companion app: each pairs an accent with a glass material and a subtle wash.
// The window is always a real behind-window blur (see VisualEffectView); themes vary the character.
enum AppTheme: String, CaseIterable, Identifiable {
    case classic, modern, github, graphite
    var id: String { rawValue }
    var label: String {
        switch self {
        case .classic: return "Classic"
        case .modern: return "Modern"
        case .github: return "GitHub"
        case .graphite: return "Graphite"
        }
    }
    // The accent that drives buttons, selection, and highlights throughout the panel.
    var accent: Color {
        switch self {
        case .classic: return Color(red: 0.22, green: 0.49, blue: 0.96)   // the original blue
        case .modern: return Color(red: 0.46, green: 0.36, blue: 0.95)    // vivid indigo
        case .github: return Color(red: 0.13, green: 0.55, blue: 0.27)    // GitHub primary green
        case .graphite: return Color(red: 0.40, green: 0.44, blue: 0.52)  // neutral slate
        }
    }
    // The most translucent vibrancy materials, so the desktop reads clearly through the panel.
    // .hudWindow and .underWindowBackground are the thinnest glass AppKit offers; all adapt.
    var material: NSVisualEffectView.Material {
        switch self {
        case .classic: return .underWindowBackground
        case .modern: return .hudWindow
        case .github: return .underWindowBackground
        case .graphite: return .hudWindow
        }
    }
    // A very faint color wash for character — kept low so it doesn't reintroduce opacity.
    var wash: Color {
        switch self {
        case .classic: return .clear
        case .modern: return Color(red: 0.46, green: 0.36, blue: 0.95).opacity(0.05)
        case .github: return .clear
        case .graphite: return .clear
        }
    }
}

// A real macOS "glass" backing: blurs the desktop/windows behind the panel (behind-window blending),
// unlike SwiftUI's in-window Materials. Behind-window vibrancy only shows through when the hosting
// window is itself non-opaque, which SwiftUI's MenuBarExtra window is not by default — so the view
// clears its window's background once attached.
struct VisualEffectView: NSViewRepresentable {
    var material: NSVisualEffectView.Material

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = TranslucentEffectView()
        view.blendingMode = .behindWindow
        view.state = .active
        view.material = material
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {
        view.material = material
    }
}

final class TranslucentEffectView: NSVisualEffectView {
    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        clearWindowBackground()
        // SwiftUI can re-set the window opaque right after we attach, so clear it again next runloop.
        DispatchQueue.main.async { [weak self] in self?.clearWindowBackground() }
    }

    private func clearWindowBackground() {
        guard let window else { return }
        window.isOpaque = false
        window.backgroundColor = .clear
    }
}

// Shared usage color bands so the menu bar, limit bars, and health gauges all agree.
// <75 calm green · 75–90 amber · ≥90 red.
func usageTint(_ percent: Double) -> Color {
    percent >= 90 ? Color(red: 0.9, green: 0.35, blue: 0.35)
        : percent >= 75 ? Color(red: 0.9, green: 0.6, blue: 0.25)
        : Color(red: 0.28, green: 0.7, blue: 0.55)
}

// A live menu-bar summary for the active provider's session limit. Fill and color are separate so
// the gauge can show budget *remaining* (a draining battery) while still coloring by usage danger.
struct MenuBarSummary {
    var text: String
    var secondary: String?
    var tint: Color
    var provider: String
    var working: Bool
    var fillPercent: Double?      // 0–100: how full the visual is — remaining budget, so it counts down
    var colorPercent: Double?     // 0–100: used %, which drives the green→amber→red band color
    var secondaryFill: Double?    // dual mode's weekly bar fill (remaining)
    var secondaryColor: Double?   // dual mode's weekly bar color (used)
}

// A terminal the companion can open for the guided setup / device management. Terminal and iTerm are
// driven by their AppleScript dictionaries (a command lands in a new window, ready to run). Terminals
// with a documented "run this command" launch flag are opened through `open --args`. Anything else
// (Warp, Hyper, …) can't be reliably told to run a command, so we open it and copy the command to the
// clipboard for the user to paste — which works for every terminal.
struct TerminalOption: Identifiable, Hashable {
    enum Launch: Hashable {
        case terminalApp                // Terminal.app dictionary
        case iterm                      // iTerm2 dictionary
        case openArgs([String])         // open -na <name> --args <these…> <command>
        case clipboard                  // open the app; user pastes the copied command
    }
    let id: String                      // stable key persisted in preferences
    let name: String                    // display name and `open -a` target
    let bundleID: String
    let launch: Launch

    // Ordered by popularity; only the installed ones are offered.
    static let all: [TerminalOption] = [
        .init(id: "terminal",  name: "Terminal",  bundleID: "com.apple.Terminal",     launch: .terminalApp),
        .init(id: "iterm",     name: "iTerm",     bundleID: "com.googlecode.iterm2",  launch: .iterm),
        .init(id: "warp",      name: "Warp",      bundleID: "dev.warp.Warp-Stable",   launch: .clipboard),
        .init(id: "ghostty",   name: "Ghostty",   bundleID: "com.mitchellh.ghostty",  launch: .openArgs(["-e", "/bin/zsh", "-lc"])),
        .init(id: "kitty",     name: "kitty",     bundleID: "net.kovidgoyal.kitty",   launch: .openArgs(["/bin/zsh", "-lc"])),
        .init(id: "alacritty", name: "Alacritty", bundleID: "org.alacritty",          launch: .openArgs(["-e", "/bin/zsh", "-lc"])),
        .init(id: "wezterm",   name: "WezTerm",   bundleID: "com.github.wez.wezterm", launch: .openArgs(["start", "--", "/bin/zsh", "-lc"])),
        .init(id: "hyper",     name: "Hyper",     bundleID: "co.zeit.hyper",          launch: .clipboard),
    ]
}

// The latest GitHub release, used by the "check for updates" flow.
struct GitHubReleaseAsset: Decodable {
    let name: String
    let browser_download_url: String
}

struct GitHubRelease: Decodable {
    let tag_name: String
    let html_url: String
    let assets: [GitHubReleaseAsset]
}

enum UpdateStatus: Equatable {
    case idle
    case checking
    case upToDate
    // asset is the app .zip to self-install; nil falls back to opening the release page.
    case available(version: String, page: URL, asset: URL?)
    case downloading(version: String)
    case failed(String)
}

// Shapes matching `kvm_ai_push.py app-usage` — this Mac's local usage snapshot. Account, cost,
// credits and extra-usage fields are local-only enrichments; they are never in the KVM push.
struct AppUsage: Decodable {
    let providers: [ProviderPayload]
    let working: [String: Bool]
    let accounts: [AccountInfo]?
    let comet: CometHealth?
}

// Comet Pro health, returned by the KVM on the usage push and cached locally by the helper.
struct CometHealth: Decodable {
    let kvmHost: String?
    let agentVersion: String?
    let fetchedAt: String?
    let system: CometSystem?
}

struct CometSystem: Decodable {
    let cpuPercent: Double?
    let memPercent: Double?
    let memUsedMb: Double?
    let memTotalMb: Double?
    let diskPercent: Double?
    let diskUsedGb: Double?
    let diskTotalGb: Double?
    let tempC: Double?
    let load1: Double?
    let uptimeSec: Double?
}

struct ProviderPayload: Decodable {
    let provider: String
    let plan: String?
    let loggedIn: Bool?
    let limits: [LimitPayload]?
    let daily: [DailyPayload]?
    let models: [ModelPayload]?
    let platforms: [PlatformPayload]?
    let account: AccountInfo?
    let extraUsage: ExtraUsage?
    let credits: CreditInfo?
    let freeResets: Int?
    let cost: CostSummary?
}

struct AccountInfo: Decodable {
    let provider: String?
    let id: String?
    let email: String?
    let level: String?
    let org: String?
    let authMode: String?
    let active: Bool?
}

struct ExtraUsage: Decodable {
    let enabled: Bool?
    let utilization: Double?
    let spendPercent: Double?
}

struct CreditInfo: Decodable {
    let balance: Double?
    let hasCredits: Bool?
    let unlimited: Bool?
}

struct CostSummary: Decodable {
    let today: Double?
    let month: Double?
    let currency: String?
    let estimated: Bool?
    let rough: Bool?
}

struct ModelPayload: Decodable {
    let model: String
    let tokens: Double
}

struct PlatformPayload: Decodable {
    let platform: String
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
    private var usageLoadedAt: Date?
    private var refreshTimer: Timer?

    // Show the codex credits / claude extra-usage sections. On by default; user can hide them.
    @Published var showExtras: Bool =
        UserDefaults.standard.object(forKey: "showExtras") as? Bool ?? true {
        didSet { UserDefaults.standard.set(showExtras, forKey: "showExtras") }
    }
    // How often to re-read local usage while the app is open, in seconds; 0 = off. This also drives
    // the KVM push cadence (see setPushInterval).
    @Published var refreshInterval: Int =
        UserDefaults.standard.object(forKey: "refreshInterval") as? Int ?? 300 {
        didSet {
            UserDefaults.standard.set(refreshInterval, forKey: "refreshInterval")
            applyRefreshInterval()
        }
    }

    // What the menu bar renders. Default to the primary-limit percentage (the CodexBar/ClaudeBar
    // signature); the user can switch to cost, a dual session/weekly readout, or the plain icon.
    @Published var menuBarMode: MenuBarMode =
        MenuBarMode(rawValue: UserDefaults.standard.string(forKey: "menuBarMode") ?? "") ?? .gauge {
        didSet { UserDefaults.standard.set(menuBarMode.rawValue, forKey: "menuBarMode") }
    }

    // The companion app's preset look (accent + glass). Persisted; drives colors panel-wide.
    @Published var appTheme: AppTheme =
        AppTheme(rawValue: UserDefaults.standard.string(forKey: "appTheme") ?? "") ?? .classic {
        didSet { UserDefaults.standard.set(appTheme.rawValue, forKey: "appTheme") }
    }

    // Native macOS alerts when the primary limit crosses a chosen percentage.
    @Published var notificationsEnabled: Bool =
        UserDefaults.standard.object(forKey: "notificationsEnabled") as? Bool ?? false {
        didSet {
            UserDefaults.standard.set(notificationsEnabled, forKey: "notificationsEnabled")
            if notificationsEnabled { requestNotificationAuth() }
        }
    }
    @Published var notificationThresholds: Set<Int> =
        MonitorModel.loadThresholds() {
        didSet {
            UserDefaults.standard.set(Array(notificationThresholds).sorted(), forKey: "notificationThresholds")
        }
    }

    private static func loadThresholds() -> Set<Int> {
        if let stored = UserDefaults.standard.array(forKey: "notificationThresholds") as? [Int] {
            return Set(stored)
        }
        return [75, 90]
    }

    @Published var updateStatus: UpdateStatus = .idle
    @Published var preferredTerminalID: String =
        UserDefaults.standard.string(forKey: "preferredTerminalID") ?? TerminalOption.all[0].id {
        didSet { UserDefaults.standard.set(preferredTerminalID, forKey: "preferredTerminalID") }
    }

    // Terminals actually present on this Mac; the picker only offers these.
    var installedTerminals: [TerminalOption] {
        TerminalOption.all.filter {
            NSWorkspace.shared.urlForApplication(withBundleIdentifier: $0.bundleID) != nil
        }
    }

    // The chosen terminal, falling back to the first installed one (Terminal always exists on macOS).
    var selectedTerminal: TerminalOption {
        let installed = installedTerminals
        return installed.first { $0.id == preferredTerminalID }
            ?? installed.first
            ?? TerminalOption.all[0]
    }

    var currentVersion: String {
        Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "?"
    }

    var updateDetail: String {
        switch updateStatus {
        case .idle: return "Check GitHub for the newest companion release."
        case .checking: return "Checking…"
        case .upToDate: return "You’re on the latest release."
        case .available(let version, _, _): return "Version \(version) is available."
        case .downloading(let version): return "Downloading \(version)…"
        case .failed(let message): return message
        }
    }

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

    init() {
        refresh()
        // Keep the menu-bar metric live even while the popover is closed: the usage timer runs off
        // the model (which outlives any panel view), not off a view's onAppear.
        applyRefreshInterval()
        loadUsage(force: true)
        checkForUpdatesIfDue()
    }

    // MARK: Menu-bar summary

    // The provider whose number the menu bar should show: the one actively working, else the one the
    // user is viewing, else the first available.
    private var menuBarProvider: ProviderPayload? {
        guard let usage = appUsage else { return nil }
        let workingId = usage.working.first(where: { $0.value })?.key
        return usage.providers.first { $0.provider == workingId }
            ?? usage.providers.first { $0.provider == usageProvider }
            ?? usage.providers.first
    }

    // The most-constrained limit (highest used %) for a provider — what "am I about to hit a wall?"
    // really means. Ties break toward the shorter window (session before weekly). Used for alerts.
    func primaryLimit(_ provider: ProviderPayload) -> LimitPayload? {
        (provider.limits ?? [])
            .filter { $0.usedPercent != nil }
            .max { lhs, rhs in
                let l = lhs.usedPercent ?? 0, r = rhs.usedPercent ?? 0
                if l != r { return l < r }
                return (lhs.windowMinutes ?? Int.max) > (rhs.windowMinutes ?? Int.max)
            }
    }

    // The session (shortest-window) limit — the "how much do I have right now?" number the menu bar
    // tracks. Falls back to any available limit so single-window providers (e.g. Codex weekly) show.
    func sessionLimit(_ provider: ProviderPayload) -> LimitPayload? {
        let withPercent = (provider.limits ?? []).filter { $0.usedPercent != nil }
        return withPercent.min { ($0.windowMinutes ?? Int.max) < ($1.windowMinutes ?? Int.max) }
    }

    var menuBarSummary: MenuBarSummary? {
        guard let provider = menuBarProvider else { return nil }
        let working = appUsage?.working[provider.provider] ?? false
        let limits = provider.limits ?? []
        switch menuBarMode {
        case .icon:
            return nil
        case .costToday:
            guard let today = provider.cost?.today else {
                // Fall back to the limit if this provider reports no cost.
                return limitSummary(provider, working: working)
            }
            return MenuBarSummary(text: String(format: "$%.2f", today), secondary: nil,
                                  tint: .primary, provider: provider.provider, working: working)
        case .dual:
            // Session (shortest window) first, weekly second — both shown as remaining (counting down).
            let session = limits.first?.usedPercent
            let weekly = limits.count > 1 ? limits[1].usedPercent : nil
            let worst = max(session ?? 0, weekly ?? 0)
            let text = session.map { "\(Int(100 - $0))%" } ?? "--"
            let secondary = weekly.map { "\(Int(100 - $0))%" }
            return MenuBarSummary(text: text, secondary: secondary, tint: usageTint(worst),
                                  provider: provider.provider, working: working,
                                  fillPercent: session.map { 100 - $0 }, colorPercent: session,
                                  secondaryFill: weekly.map { 100 - $0 }, secondaryColor: weekly)
        case .limitPercent, .gauge:
            return limitSummary(provider, working: working)
        }
    }

    // Session budget as *remaining* (100 − used) so the gauge reads as a countdown/draining battery,
    // colored by usage so it still goes amber → red as the session fills.
    private func limitSummary(_ provider: ProviderPayload, working: Bool) -> MenuBarSummary? {
        guard let limit = sessionLimit(provider), let used = limit.usedPercent else {
            return MenuBarSummary(text: "--", secondary: nil, tint: .primary,
                                  provider: provider.provider, working: working,
                                  fillPercent: nil, colorPercent: nil)
        }
        let remaining = max(0, 100 - used)
        return MenuBarSummary(text: "\(Int(remaining))%", secondary: nil, tint: usageTint(used),
                              provider: provider.provider, working: working,
                              fillPercent: remaining, colorPercent: used)
    }

    // MARK: Burn-rate projection

    // How fast a limit is filling and when, at this pace, it would reach 100%. Uses the window length
    // and the reset time to recover elapsed time; nil when the window isn't time-boxed or is idle.
    func projection(_ limit: LimitPayload) -> (perHour: Double, timeToLimit: String?)? {
        guard let percent = limit.usedPercent, percent > 0,
              let windowMinutes = limit.windowMinutes, windowMinutes > 0,
              let remaining = minutesUntilReset(limit.resetsAt) else { return nil }
        let elapsed = Double(windowMinutes) - remaining
        guard elapsed >= 5 else { return nil }        // too early to trend meaningfully
        let perMinute = percent / elapsed
        guard perMinute > 0 else { return nil }
        let perHour = perMinute * 60
        if percent >= 100 { return (perHour, "limit reached") }
        let minutesToFull = (100 - percent) / perMinute
        // Only warn when the wall arrives before the window resets; otherwise you're safely within.
        if minutesToFull >= remaining { return (perHour, nil) }
        return (perHour, "≈ full in \(shortDuration(minutesToFull))")
    }

    private func minutesUntilReset(_ iso: String?) -> Double? {
        guard let iso else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = formatter.date(from: iso)
        if date == nil { formatter.formatOptions = [.withInternetDateTime]; date = formatter.date(from: iso) }
        guard let date else { return nil }
        return max(0, date.timeIntervalSinceNow / 60)
    }

    private func shortDuration(_ minutes: Double) -> String {
        if minutes >= 2880 { return "\(Int((minutes / 1440).rounded()))d" }
        if minutes >= 120 { return "\(Int((minutes / 60).rounded()))h" }
        if minutes >= 1 { return "\(Int(minutes.rounded()))m" }
        return "<1m"
    }

    // MARK: Threshold notifications

    private func requestNotificationAuth() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    // Fire once when the active provider's primary limit crosses an enabled threshold. Keyed by
    // provider|label|resetsAt so a new window (new reset time) naturally re-arms every threshold.
    private func evaluateNotifications() {
        guard notificationsEnabled, !notificationThresholds.isEmpty else { return }
        guard let provider = menuBarProvider, let limit = primaryLimit(provider),
              let percent = limit.usedPercent else { return }
        let windowKey = "\(provider.provider)|\(limit.label ?? "limit")|\(limit.resetsAt ?? "")"
        var fired = (UserDefaults.standard.dictionary(forKey: "firedThresholds") as? [String: [Int]]) ?? [:]
        // Drop stale windows so the store doesn't grow without bound.
        if fired[windowKey] == nil { fired = [windowKey: fired[windowKey] ?? []] }
        var already = Set(fired[windowKey] ?? [])
        for threshold in notificationThresholds.sorted() where Int(percent) >= threshold && !already.contains(threshold) {
            already.insert(threshold)
            postLimitNotification(provider: provider, limit: limit, threshold: threshold, percent: percent)
        }
        fired[windowKey] = Array(already).sorted()
        UserDefaults.standard.set(fired, forKey: "firedThresholds")
    }

    private func postLimitNotification(provider: ProviderPayload, limit: LimitPayload, threshold: Int, percent: Double) {
        let content = UNMutableNotificationContent()
        let name = Self.providerName(provider.provider)
        let window = (limit.label ?? "limit").lowercased()
        content.title = "\(name) \(window) at \(Int(percent))%"
        if let reset = resetRelative(limit.resetsAt) {
            content.body = "You've passed \(threshold)% — \(reset)."
        } else {
            content.body = "You've passed \(threshold)% of your \(window)."
        }
        content.sound = threshold >= 90 ? .default : nil
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

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
        // Always land on the latest published formula. `reinstall` (not `upgrade`) is deliberate: the
        // CLI was renumbered from a 1.x line down to 0.x, so Homebrew treats a stale 1.6.0 as "newer"
        // and would refuse to upgrade — reinstall pulls the current tag regardless of version order.
        let command = "if command -v brew >/dev/null 2>&1; then if brew list kvm-ai-monitor >/dev/null 2>&1; then brew reinstall ivangong24/kvm-ai-monitor/kvm-ai-monitor; else brew install ivangong24/kvm-ai-monitor/kvm-ai-monitor; fi; fi; kvm-ai-monitor"
        openInTerminal(command, clipboardNotice: "Setup command copied — paste it into \(selectedTerminal.name) and press Return.")
    }

    // Self-install the update: download the release .app archive, hand a small detached helper the
    // job of swapping this bundle once we quit, then relaunch. No Homebrew — works for any install
    // location. Falls back to the release page when the release has no downloadable archive.
    func installUpdate(version: String, asset: URL?, page: URL) {
        guard let asset else {
            NSWorkspace.shared.open(page)
            return
        }
        if case .downloading = updateStatus { return }
        updateStatus = .downloading(version: version)
        notice = nil
        let destPath = Bundle.main.bundlePath
        Task {
            // nil means the swap helper is staged and running; any string is a failure reason.
            if let failure = await Self.performSelfUpdate(asset: asset, destPath: destPath) {
                updateStatus = .available(version: version, page: page, asset: asset)
                notice = "Update failed: \(failure). Try again, or download it from the release page."
            } else {
                // The helper waits for us to exit, then swaps the bundle and reopens the new app.
                notice = "Installing update — the app will relaunch."
                NSApp.terminate(nil)
            }
        }
    }

    // Runs off the main actor: download, unpack, and stage the swap helper. Returns nil once the
    // helper is launched (the bundle replacement happens after the app terminates), else a reason.
    nonisolated private static func performSelfUpdate(asset: URL, destPath: String) async -> String? {
        var request = URLRequest(url: asset)
        request.setValue("KVM-AI-Monitor", forHTTPHeaderField: "User-Agent")
        request.timeoutInterval = 120
        let downloadedZip: URL
        do {
            let (temp, response) = try await URLSession.shared.download(for: request)
            let code = (response as? HTTPURLResponse)?.statusCode ?? -1
            guard code == 200 else { return "download failed (HTTP \(code))" }
            downloadedZip = temp
        } catch {
            return error.localizedDescription
        }

        let work = FileManager.default.temporaryDirectory
            .appendingPathComponent("kvm-ai-update-\(UUID().uuidString)")
        let extractDir = work.appendingPathComponent("extract")
        do {
            try FileManager.default.createDirectory(at: extractDir, withIntermediateDirectories: true)
        } catch {
            return "could not prepare the update"
        }
        // ditto reads Apple's zip layout (what the release workflow produces) correctly.
        guard Self.run("/usr/bin/ditto", ["-x", "-k", downloadedZip.path, extractDir.path]).status == 0 else {
            return "could not unpack the update"
        }
        guard let appName = (try? FileManager.default.contentsOfDirectory(atPath: extractDir.path))?
            .first(where: { $0.hasSuffix(".app") }) else {
            return "the update archive contained no app"
        }
        let newApp = extractDir.appendingPathComponent(appName).path

        let script = work.appendingPathComponent("swap.sh")
        let scriptBody = """
        #!/bin/sh
        # Args: <pid> <dest-bundle> <new-app> <work-dir>. Wait for the app to quit, replace its
        # bundle (rolling back on failure), relaunch, then clean up.
        PID="$1"; DEST="$2"; NEW="$3"; WORK="$4"
        i=0
        while kill -0 "$PID" 2>/dev/null && [ "$i" -lt 300 ]; do sleep 0.2; i=$((i + 1)); done
        sleep 0.3
        xattr -dr com.apple.quarantine "$NEW" 2>/dev/null
        rm -rf "$DEST.kvm-old"
        if [ -d "$DEST" ]; then mv "$DEST" "$DEST.kvm-old" 2>/dev/null; fi
        if ditto "$NEW" "$DEST"; then
            rm -rf "$DEST.kvm-old"
        else
            rm -rf "$DEST"
            [ -d "$DEST.kvm-old" ] && mv "$DEST.kvm-old" "$DEST"
        fi
        open "$DEST"
        rm -rf "$WORK"
        """
        do {
            try scriptBody.write(to: script, atomically: true, encoding: .utf8)
        } catch {
            return "could not stage the updater"
        }
        let pid = String(ProcessInfo.processInfo.processIdentifier)
        guard Self.spawnDetached("/bin/sh", [script.path, pid, destPath, newApp, work.path]) else {
            return "could not start the updater"
        }
        return nil
    }

    // Launch a process without waiting; it outlives us so it can swap the bundle after we quit.
    nonisolated private static func spawnDetached(_ tool: String, _ arguments: [String]) -> Bool {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: tool)
        process.arguments = arguments
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run(); return true } catch { return false }
    }

    // Run a shell command in the user's chosen terminal, reused by the setup and update flows.
    private func openInTerminal(_ command: String, clipboardNotice: String) {
        let terminal = selectedTerminal
        notice = nil
        switch terminal.launch {
        case .terminalApp:
            runAppleScript("""
            tell application "Terminal"
                activate
                do script "\(escapeForAppleScript(command))"
            end tell
            """)
        case .iterm:
            runAppleScript("""
            tell application "iTerm"
                activate
                set newWindow to (create window with default profile)
                tell current session of newWindow to write text "\(escapeForAppleScript(command))"
            end tell
            """)
        case .openArgs(let prefix):
            Task.detached {
                _ = Self.run("/usr/bin/open", ["-na", terminal.name, "--args"] + prefix + [command])
            }
        case .clipboard:
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(command, forType: .string)
            _ = Self.run("/usr/bin/open", ["-a", terminal.name])
            notice = clipboardNotice
        }
    }

    private func runAppleScript(_ source: String) {
        var error: NSDictionary?
        NSAppleScript(source: source)?.executeAndReturnError(&error)
        if error != nil {
            notice = "Couldn’t open \(selectedTerminal.name). Allow control in System Settings › Privacy › Automation, or pick a different terminal."
        }
    }

    // AppleScript string literals need backslashes and quotes escaped so the command survives verbatim.
    private func escapeForAppleScript(_ text: String) -> String {
        text.replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
    }

    // Last time an automatic check ran, so launch + panel-open triggers don't hammer the GitHub API.
    private var lastUpdateCheck: Date?

    // Automatic detection: check on launch and when the panel opens, but at most once every few hours,
    // and never clobber a pending manual check or an already-surfaced available update.
    func checkForUpdatesIfDue() {
        switch updateStatus {
        case .checking, .available, .downloading: return
        default: break
        }
        if let last = lastUpdateCheck, Date().timeIntervalSince(last) < 6 * 3600 { return }
        checkForUpdates()
    }

    func checkForUpdates() {
        if case .checking = updateStatus { return }
        updateStatus = .checking
        lastUpdateCheck = Date()
        let current = currentVersion
        Task {
            let result = await Self.fetchLatestRelease()
            switch result {
            case .success(let release):
                let latest = release.tag_name.hasPrefix("v")
                    ? String(release.tag_name.dropFirst()) : release.tag_name
                if Self.isVersion(latest, newerThan: current), let page = URL(string: release.html_url) {
                    // Prefer the .app archive so "Get update" can self-install; nil -> open the page.
                    let asset = release.assets
                        .first { $0.name.hasSuffix(".zip") }
                        .flatMap { URL(string: $0.browser_download_url) }
                    updateStatus = .available(version: latest, page: page, asset: asset)
                } else {
                    updateStatus = .upToDate
                }
            case .failure:
                updateStatus = .failed("Couldn’t reach GitHub. Check your connection and try again.")
            }
        }
    }

    nonisolated private static func fetchLatestRelease() async -> Result<GitHubRelease, Error> {
        let url = URL(string: "https://api.github.com/repos/ivangong24/kvm_AI_monitor/releases/latest")!
        var request = URLRequest(url: url)
        request.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        request.setValue("KVM-AI-Monitor", forHTTPHeaderField: "User-Agent")
        request.timeoutInterval = 15
        do {
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                return .failure(URLError(.badServerResponse))
            }
            return .success(try JSONDecoder().decode(GitHubRelease.self, from: data))
        } catch {
            return .failure(error)
        }
    }

    // Numeric semver compare ("0.8.0" newer than "0.7.0"); non-numeric suffixes are ignored.
    nonisolated static func isVersion(_ candidate: String, newerThan base: String) -> Bool {
        func parts(_ text: String) -> [Int] {
            text.split(separator: ".").map { Int($0.prefix { $0.isNumber }) ?? 0 }
        }
        let lhs = parts(candidate), rhs = parts(base)
        for index in 0..<max(lhs.count, rhs.count) {
            let left = index < lhs.count ? lhs[index] : 0
            let right = index < rhs.count ? rhs[index] : 0
            if left != right { return left > right }
        }
        return false
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
        // Reload when forced, when never loaded, or when the cached snapshot is stale (>15s), so the
        // live view and charts stay current on reopen / refresh instead of showing the first fetch.
        let fresh = usageLoadedAt.map { Date().timeIntervalSince($0) < 15 } ?? false
        if appUsage != nil && !force && fresh { return }
        let script = helperScript.path
        usageLoading = true
        usageError = nil
        Task {
            let result = await Task.detached {
                Self.run("/usr/bin/env", ["python3", script, "app-usage"])
            }.value
            usageLoading = false
            usageLoadedAt = Date()
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
                evaluateNotifications()
            } else {
                usageError = "Couldn’t read usage from this Mac."
            }
        }
    }

    // MARK: Refresh interval

    // Restart the auto-refresh timer to match the chosen interval (0 = off). Called on change and
    // when the Usage tab appears. Each tick re-reads local usage so the view stays current.
    func applyRefreshInterval() {
        refreshTimer?.invalidate()
        refreshTimer = nil
        guard refreshInterval > 0 else { return }
        refreshTimer = Timer.scheduledTimer(withTimeInterval: Double(refreshInterval), repeats: true) { [weak self] _ in
            Task { @MainActor in self?.loadUsage(force: true) }
        }
    }

    // Match the KVM push cadence to the app's refresh interval (clamped by the helper to ≥30s).
    func setPushInterval(_ seconds: Int) {
        guard helperInstalled, seconds > 0 else { return }
        let script = helperScript.path
        Task.detached {
            _ = Self.run("/usr/bin/env", ["python3", script, "set-push-interval", String(seconds)])
        }
    }

    // MARK: Accounts

    var detectedAccounts: [AccountInfo] { appUsage?.accounts ?? [] }

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

// AppKit twin of usageTint, for drawing the gauge fill into an NSImage.
func nsUsageTint(_ percent: Double) -> NSColor {
    percent >= 90 ? NSColor(red: 0.9, green: 0.35, blue: 0.35, alpha: 1)
        : percent >= 75 ? NSColor(red: 0.9, green: 0.6, blue: 0.25, alpha: 1)
        : NSColor(red: 0.28, green: 0.7, blue: 0.55, alpha: 1)
}

private let gaugeIconSize = NSSize(width: 20, height: 14)
private let gaugeScreenRect = NSRect(x: 3.3, y: 3.1, width: 13.4, height: 7.8)

// The battery frame + empty track as a template, so it adopts the menu-bar (or panel) foreground
// color in both light and dark. Drawn as two NSImages because a SwiftUI Canvas does not render
// inside a MenuBarExtra label — only Image/Text do.
func gaugeFrameIcon() -> NSImage {
    let image = NSImage(size: gaugeIconSize)
    image.lockFocus()
    NSColor.black.setStroke()
    let body = NSBezierPath(roundedRect: NSRect(x: 1.1, y: 1.0, width: 17.8, height: 12.0),
                            xRadius: 2.4, yRadius: 2.4)
    body.lineWidth = 1.3
    body.stroke()
    // Faint track: template art keeps the alpha, so this reads as a subtle tint of the fg color.
    NSColor(white: 0, alpha: 0.16).setFill()
    NSBezierPath(roundedRect: gaugeScreenRect, xRadius: 1.1, yRadius: 1.1).fill()
    image.unlockFocus()
    image.isTemplate = true
    return image
}

// The colored fill bar: width = `fillPercent` (remaining), color from `colorPercent` (used). Non-
// template so the band color survives.
func gaugeFillIcon(fillPercent: Double, colorPercent: Double) -> NSImage {
    let image = NSImage(size: gaugeIconSize)
    image.lockFocus()
    NSBezierPath(roundedRect: gaugeScreenRect, xRadius: 1.1, yRadius: 1.1).addClip()
    let clamped = max(0, min(1, fillPercent / 100))
    nsUsageTint(colorPercent).setFill()
    NSBezierPath(rect: NSRect(x: gaugeScreenRect.minX, y: gaugeScreenRect.minY,
                              width: gaugeScreenRect.width * clamped,
                              height: gaugeScreenRect.height)).fill()
    image.unlockFocus()
    return image
}

// --- Ring gauge (Limit %) ---------------------------------------------------------------------
private let ringIconSize = NSSize(width: 15, height: 14)
private let ringCenter = NSPoint(x: 7.5, y: 7)
private let ringRadius: CGFloat = 5.0

// Faint full-circle track as a template, so it adopts the menu-bar/panel foreground color.
func ringTrackIcon() -> NSImage {
    let image = NSImage(size: ringIconSize)
    image.lockFocus()
    let ring = NSBezierPath()
    ring.appendArc(withCenter: ringCenter, radius: ringRadius, startAngle: 0, endAngle: 360)
    ring.lineWidth = 2.2
    NSColor(white: 0, alpha: 0.28).setStroke()
    ring.stroke()
    image.unlockFocus()
    image.isTemplate = true
    return image
}

// Colored arc from 12 o'clock, clockwise, covering `fillPercent` (remaining) of the circle, colored
// by `colorPercent` (used).
func ringArcIcon(fillPercent: Double, colorPercent: Double) -> NSImage {
    let image = NSImage(size: ringIconSize)
    image.lockFocus()
    let clamped = max(0, min(1, fillPercent / 100))
    if clamped > 0.001 {
        let arc = NSBezierPath()
        arc.appendArc(withCenter: ringCenter, radius: ringRadius,
                      startAngle: 90, endAngle: 90 - clamped * 360, clockwise: true)
        arc.lineWidth = 2.2
        arc.lineCapStyle = .round
        nsUsageTint(colorPercent).setStroke()
        arc.stroke()
    }
    image.unlockFocus()
    return image
}

// --- Dual bars (Session + weekly) -------------------------------------------------------------
private let dualIconSize = NSSize(width: 14, height: 14)
private let dualBarXs: [CGFloat] = [2.5, 8.0]
private let dualBarWidth: CGFloat = 3.5
private let dualBarBottom: CGFloat = 2
private let dualBarMaxHeight: CGFloat = 10

func dualTrackIcon() -> NSImage {
    let image = NSImage(size: dualIconSize)
    image.lockFocus()
    NSColor(white: 0, alpha: 0.22).setFill()
    for x in dualBarXs {
        NSBezierPath(roundedRect: NSRect(x: x, y: dualBarBottom, width: dualBarWidth,
                                         height: dualBarMaxHeight), xRadius: 1.6, yRadius: 1.6).fill()
    }
    image.unlockFocus()
    image.isTemplate = true
    return image
}

func dualFillIcon(sessionFill: Double?, sessionColor: Double?,
                  weeklyFill: Double?, weeklyColor: Double?) -> NSImage {
    let image = NSImage(size: dualIconSize)
    image.lockFocus()
    let bars = [(dualBarXs[0], sessionFill, sessionColor), (dualBarXs[1], weeklyFill, weeklyColor)]
    for (x, fill, color) in bars {
        guard let fill, let color else { continue }
        let height = max(1.6, dualBarMaxHeight * max(0, min(1, fill / 100)))
        nsUsageTint(color).setFill()
        NSBezierPath(roundedRect: NSRect(x: x, y: dualBarBottom, width: dualBarWidth,
                                         height: height), xRadius: 1.6, yRadius: 1.6).fill()
    }
    image.unlockFocus()
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

private struct HealthBar: View {
    let label: String
    let percent: Double
    let detail: String
    private var clamped: Double { max(0, min(100, percent)) }
    private var tint: Color { usageTint(clamped) }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(label).font(.system(size: 11, weight: .semibold))
                Spacer()
                Text(detail).font(.system(size: 10)).foregroundStyle(.secondary)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.08))
                    Capsule().fill(tint).frame(width: max(3, geo.size.width * clamped / 100))
                }
            }
            .frame(height: 6)
        }
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

// A compact on/off percentage pill for choosing notification thresholds.
private struct ThresholdChip: View {
    let threshold: Int
    let on: Bool
    var accent: Color = Color(red: 0.22, green: 0.49, blue: 0.96)
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Text("\(threshold)")
                .font(.system(size: 11, weight: .semibold))
                .monospacedDigit()
                .foregroundStyle(on ? .white : Color.secondary)
                .frame(width: 30, height: 24)
                .background(on ? accent : Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 7))
                .overlay(RoundedRectangle(cornerRadius: 7).stroke(on ? .clear : Color.primary.opacity(0.12)))
        }
        .buttonStyle(.plain)
        .help(on ? "Alert at \(threshold)% is on" : "Alert at \(threshold)% is off")
    }
}

struct CompanionPanel: View {
    @ObservedObject var model: MonitorModel

    private var accent: Color { model.appTheme.accent }

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
        .background(
            VisualEffectView(material: model.appTheme.material)
                .overlay(model.appTheme.wash)
                .ignoresSafeArea()
        )
        .tint(accent)
        .onAppear { model.refresh(); model.loadUsage(force: true); model.checkForUpdatesIfDue() }
        .onReceive(Timer.publish(every: 20, on: .main, in: .common).autoconnect()) { _ in
            model.refresh()
            model.loadUsage()
        }
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
            touchscreenSection
            cometHealthSection
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

    @ViewBuilder
    private var touchscreenSection: some View {
        if let usage = model.appUsage, let first = usage.providers.first {
            let workingId = usage.working.first(where: { $0.value })?.key
            let provider = usage.providers.first(where: { $0.provider == workingId })
                ?? usage.providers.first(where: { $0.provider == model.usageProvider })
                ?? first
            VStack(alignment: .leading, spacing: 8) {
                Text("COMET PRO TOUCHSCREEN")
                    .font(.system(size: 9, weight: .bold)).tracking(0.9).foregroundStyle(.secondary)
                TouchscreenCard(provider: provider, working: usage.working[provider.provider] ?? false)
            }
        }
    }

    @ViewBuilder
    private var cometHealthSection: some View {
        if let system = model.appUsage?.comet?.system {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("COMET PRO HEALTH")
                        .font(.system(size: 9, weight: .bold)).tracking(0.9).foregroundStyle(.secondary)
                    Spacer()
                    if let version = model.appUsage?.comet?.agentVersion {
                        Text("agent \(version)").font(.system(size: 9, weight: .semibold)).foregroundStyle(.secondary)
                    }
                }
                VStack(spacing: 10) {
                    if let cpu = system.cpuPercent {
                        HealthBar(label: "CPU", percent: cpu, detail: "\(Int(cpu))%")
                    }
                    if let mem = system.memPercent {
                        HealthBar(label: "Memory", percent: mem,
                                  detail: memDetail(system) ?? "\(Int(mem))%")
                    }
                    if let disk = system.diskPercent {
                        HealthBar(label: "Disk", percent: disk,
                                  detail: diskDetail(system) ?? "\(Int(disk))%")
                    }
                    HStack(spacing: 18) {
                        if let temp = system.tempC { healthStat("Temp", String(format: "%.0f°C", temp)) }
                        if let load = system.load1 { healthStat("Load", String(format: "%.2f", load)) }
                        if let uptime = system.uptimeSec { healthStat("Uptime", uptimeText(uptime)) }
                        Spacer()
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 13))
                .overlay(RoundedRectangle(cornerRadius: 13).stroke(Color.primary.opacity(0.07)))
            }
        }
    }

    private func memDetail(_ s: CometSystem) -> String? {
        guard let used = s.memUsedMb, let total = s.memTotalMb, total > 0 else { return nil }
        return String(format: "%.1f / %.1f GB", used / 1024, total / 1024)
    }

    private func diskDetail(_ s: CometSystem) -> String? {
        guard let used = s.diskUsedGb, let total = s.diskTotalGb, total > 0 else { return nil }
        return String(format: "%.0f / %.0f GB", used, total)
    }

    private func uptimeText(_ seconds: Double) -> String {
        let days = Int(seconds) / 86400, hours = (Int(seconds) % 86400) / 3600
        if days > 0 { return "\(days)d \(hours)h" }
        let minutes = (Int(seconds) % 3600) / 60
        return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    }

    private func healthStat(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label.uppercased()).font(.system(size: 8.5, weight: .bold)).tracking(0.5).foregroundStyle(.secondary)
            Text(value).font(.system(size: 13, weight: .semibold))
        }
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
                SettingRow(title: "Theme",
                           detail: "The companion app's look. Every preset sits on a frosted-glass background.") {
                    themeSelector
                }
                Divider()
                SettingRow(title: "Open at login",
                           detail: "Keep the Comet Pro screen updating whenever your Mac is on.") {
                    Toggle("", isOn: Binding(get: { model.launchAtLogin }, set: { _ in model.toggleLaunchAtLogin() }))
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
                Divider()
                SettingRow(title: "Show credits & extra usage",
                           detail: "Display Codex credits and Claude extra-usage sections on the Usage tab.") {
                    Toggle("", isOn: Binding(get: { model.showExtras }, set: { model.showExtras = $0 }))
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
                Divider()
                SettingRow(title: "Refresh interval",
                           detail: "How often usage refreshes here and updates on your Comet Pro.") {
                    Picker("", selection: Binding(get: { model.refreshInterval },
                                                  set: { model.refreshInterval = $0; model.setPushInterval($0) })) {
                        Text("Off").tag(0)
                        Text("1 min").tag(60)
                        Text("5 min").tag(300)
                        Text("15 min").tag(900)
                    }
                    .labelsHidden().pickerStyle(.menu).frame(maxWidth: 110)
                }
                Divider()
                SettingRow(title: "Menu bar shows",
                           detail: "What appears in the macOS menu bar. Each metric is drawn live — a battery gauge, a ring, twin bars, or a cost badge — and recolors as you approach a limit.") {
                    HStack(spacing: 8) {
                        MenuBarContent(model: model, colored: true)
                            .padding(.horizontal, 7).padding(.vertical, 4)
                            .background(Color.primary.opacity(0.06), in: Capsule())
                            .overlay(Capsule().stroke(Color.primary.opacity(0.08)))
                        Picker("", selection: Binding(get: { model.menuBarMode }, set: { model.menuBarMode = $0 })) {
                            ForEach(MenuBarMode.allCases) { Text($0.label).tag($0) }
                        }
                        .labelsHidden().pickerStyle(.menu).frame(maxWidth: 140)
                    }
                }
            }

            settingsGroup(title: "Notifications") {
                SettingRow(title: "Limit alerts",
                           detail: "Get a macOS notification when your most-constrained limit passes a threshold.") {
                    Toggle("", isOn: Binding(get: { model.notificationsEnabled }, set: { model.notificationsEnabled = $0 }))
                        .labelsHidden()
                        .toggleStyle(.switch)
                }
                if model.notificationsEnabled {
                    Divider()
                    SettingRow(title: "Alert at",
                               detail: "Pick the percentages that trigger an alert. Each fires once per limit window.") {
                        HStack(spacing: 6) {
                            ForEach([25, 50, 75, 90], id: \.self) { threshold in
                                ThresholdChip(threshold: threshold,
                                              on: model.notificationThresholds.contains(threshold),
                                              accent: accent) {
                                    if model.notificationThresholds.contains(threshold) {
                                        model.notificationThresholds.remove(threshold)
                                    } else {
                                        model.notificationThresholds.insert(threshold)
                                    }
                                }
                            }
                        }
                    }
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
                if model.installedTerminals.count > 1 {
                    Divider()
                    SettingRow(title: "Open setup in",
                               detail: "Choose which terminal runs the guided setup and device management.") {
                        Picker("", selection: Binding(get: { model.preferredTerminalID },
                                                      set: { model.preferredTerminalID = $0 })) {
                            ForEach(model.installedTerminals) { Text($0.name).tag($0.id) }
                        }
                        .labelsHidden()
                        .pickerStyle(.menu)
                        .frame(maxWidth: 130)
                    }
                }
            }

            settingsGroup(title: "About") {
                SettingRow(title: "Version \(model.currentVersion)", detail: model.updateDetail) {
                    switch model.updateStatus {
                    case .checking, .downloading:
                        ProgressView().controlSize(.small)
                    case .available(let version, let page, let asset):
                        Button { model.installUpdate(version: version, asset: asset, page: page) } label: {
                            Label("Get update", systemImage: "arrow.down.circle.fill")
                        }
                        .buttonStyle(.borderedProminent)
                        .controlSize(.small)
                    default:
                        Button("Check for updates") { model.checkForUpdates() }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                    }
                }
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

    // Selectable theme swatches: a mini window mockup per preset (accent title bar + content lines),
    // with a ring on the active one. Tapping applies the theme immediately.
    @ViewBuilder private var themeSelector: some View {
        HStack(spacing: 7) {
            ForEach(AppTheme.allCases) { theme in
                Button { model.appTheme = theme } label: {
                    VStack(spacing: 0) {
                        Rectangle().fill(theme.accent).frame(height: 7)
                        VStack(alignment: .leading, spacing: 2) {
                            RoundedRectangle(cornerRadius: 1).fill(theme.accent.opacity(0.5)).frame(width: 14, height: 2)
                            RoundedRectangle(cornerRadius: 1).fill(Color.primary.opacity(0.25)).frame(width: 20, height: 2)
                        }
                        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                        .padding(3)
                    }
                    .frame(width: 30, height: 26)
                    .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 6))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                    .overlay(RoundedRectangle(cornerRadius: 6).stroke(Color.primary.opacity(0.12)))
                    .overlay(
                        RoundedRectangle(cornerRadius: 8)
                            .stroke(theme.accent, lineWidth: 2).padding(-2.5)
                            .opacity(model.appTheme == theme ? 1 : 0)
                    )
                }
                .buttonStyle(.plain)
                .help(theme.label)
            }
        }
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
            Button { model.refresh(); model.loadUsage(force: true) } label: { Image(systemName: "arrow.clockwise") }
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
    let lower = raw.lowercased().replacingOccurrences(of: "openai/", with: "")
    // Codex GPT-5.x models: keep the version and any codename/variant (Sol / Terra / Luna / Codex …).
    if lower.hasPrefix("gpt-") {
        let parts = lower.split(separator: "-").map(String.init)
        guard parts.count >= 2 else { return "GPT" }
        var name = "GPT-\(parts[1])"
        let suffixes = ["sol": "Sol", "terra": "Terra", "luna": "Luna", "codex": "Codex",
                        "mini": "Mini", "nano": "Nano", "pro": "Pro", "max": "Max", "spark": "Spark"]
        for part in parts.dropFirst(2) where suffixes[part] != nil { name += " \(suffixes[part]!)" }
        return name
    }
    let families: [(String, String)] = [
        ("opus", "Opus"), ("sonnet", "Sonnet"), ("haiku", "Haiku"), ("fable", "Fable"),
        ("gemini", "Gemini"), ("grok", "Grok"),
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

// Transcript `entrypoint` (Claude) or session originator (Codex) → friendly surface name.
func prettyPlatform(_ raw: String) -> String {
    let lower = raw.lowercased()
    switch lower {
    case "cli", "codex-cli", "codex-tui": return "CLI"
    case "claude-desktop", "codex-desktop": return "Desktop app"
    case "sdk-cli", "sdk": return "SDK"
    case "codex-exec": return "Automation"
    default:
        if lower.contains("vscode") || lower.contains("vs-code") { return "VS Code" }
        if lower.contains("ide") || lower.contains("jetbrains") { return "IDE" }
        if lower.contains("desktop") || lower.contains("app") { return "Desktop app" }
        return raw.replacingOccurrences(of: "codex-", with: "").replacingOccurrences(of: "-", with: " ").capitalized
    }
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
    var projection: String? = nil
    // When true, the bar recolors green→amber→red by fill; otherwise it uses the passed tint.
    var autoTint: Bool = false

    var body: some View {
        let percent = limit.usedPercent ?? 0
        let barTint = autoTint ? usageTint(percent) : tint
        VStack(alignment: .leading, spacing: 5) {
            HStack {
                Text(title.uppercased()).font(.system(size: 10, weight: .semibold)).tracking(0.4).foregroundStyle(.secondary)
                Spacer()
                Text(limit.usedPercent == nil ? "--" : "\(Int(percent))%").font(.system(size: 12, weight: .semibold))
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(Color.primary.opacity(0.09)).frame(height: 8)
                    Capsule().fill(barTint).frame(width: max(8, geo.size.width * CGFloat(min(100, percent)) / 100), height: 8)
                }
            }
            .frame(height: 8)
            HStack(spacing: 6) {
                if let reset = resetRelative(limit.resetsAt) {
                    Text(reset).font(.system(size: 10)).foregroundStyle(.secondary)
                }
                if let projection {
                    if resetRelative(limit.resetsAt) != nil {
                        Text("·").font(.system(size: 10)).foregroundStyle(.secondary)
                    }
                    Text(projection).font(.system(size: 10, weight: .medium)).foregroundStyle(usageTint(percent))
                }
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

// A circular usage meter for the "at a glance" hero: filled arc + centered percentage.
private struct HeroRing: View {
    let percent: Double
    let caption: String
    private var clamped: Double { max(0, min(100, percent)) }

    var body: some View {
        ZStack {
            Circle().stroke(Color.primary.opacity(0.09), lineWidth: 9)
            Circle()
                .trim(from: 0, to: CGFloat(clamped / 100))
                .stroke(usageTint(clamped), style: StrokeStyle(lineWidth: 9, lineCap: .round))
                .rotationEffect(.degrees(-90))
            VStack(spacing: 0) {
                Text("\(Int(clamped))%").font(.system(size: 20, weight: .bold, design: .rounded))
                Text(caption.uppercased()).font(.system(size: 7.5, weight: .bold)).tracking(0.4)
                    .foregroundStyle(.secondary).lineLimit(1)
            }
        }
        .frame(width: 74, height: 74)
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
    private var accent: Color { model.appTheme.accent }

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
                if model.primaryLimit(provider) != nil { atGlanceCard(provider) }
                accountCard(provider)
                if provider.cost != nil { costCard(provider) }
                limitsCard(provider)
                if model.showExtras {
                    if provider.extraUsage != nil { extraUsageCard(provider) }
                    if provider.credits != nil || provider.freeResets != nil { creditsCard(provider) }
                }
                dailyCard(provider)
                modelsCard(provider)
                platformsCard(provider)
            } else if model.usageLoading {
                loadingView
            } else {
                infoCard("No usage yet", model.usageError ?? "Sign in to Claude or Codex on this Mac.")
            }
        }
        .padding(16)
        .onAppear { model.loadUsage(); model.applyRefreshInterval() }
    }

    // Detected accounts for this provider (usually one; codex may add an API-key entry).
    private func accounts(for provider: String) -> [AccountInfo] {
        model.detectedAccounts.filter { $0.provider == provider }
    }

    private func accountCard(_ provider: ProviderPayload) -> some View {
        let list = accounts(for: provider.provider)
        let email = provider.account?.email ?? list.first(where: { $0.email != nil })?.email
        let level = provider.account?.level ?? provider.plan
        let working = model.appUsage?.working[provider.provider] ?? false
        let statusColor = working ? Color.green : Color.secondary
        return card(MonitorModel.providerName(provider.provider)) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    Image(systemName: "person.crop.circle").foregroundStyle(accent)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(email ?? "Signed in").font(.system(size: 13, weight: .semibold))
                        if let level { Text(level.uppercased()).font(.system(size: 10, weight: .bold)).tracking(0.5).foregroundStyle(accent) }
                    }
                    Spacer()
                    HStack(spacing: 6) {
                        Circle().fill(statusColor).frame(width: 7, height: 7)
                        Text(working ? "WORKING" : "READY").font(.system(size: 10, weight: .bold)).tracking(0.6)
                    }
                    .foregroundStyle(statusColor)
                    .padding(.horizontal, 10).padding(.vertical, 6)
                    .background(statusColor.opacity(0.14), in: Capsule())
                }
                if list.count > 1 {
                    Divider()
                    ForEach(list, id: \.id) { account in
                        HStack(spacing: 6) {
                            Image(systemName: account.active == true ? "checkmark.circle.fill" : "circle")
                                .font(.system(size: 11))
                                .foregroundStyle(account.active == true ? accent : Color.secondary)
                            Text(account.email ?? (account.authMode.map { $0.capitalized } ?? "Account"))
                                .font(.system(size: 12))
                            if let mode = account.authMode {
                                Text(mode == "apikey" ? "API key" : "ChatGPT")
                                    .font(.system(size: 9, weight: .semibold))
                                    .foregroundStyle(.secondary)
                            }
                        }
                    }
                    Text("Tracks whichever login the \(MonitorModel.providerName(provider.provider)) CLI is signed in with. Switch logins in the CLI to change it.")
                        .font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func costCard(_ provider: ProviderPayload) -> some View {
        let cost = provider.cost
        return card("Estimated cost") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 24) {
                    costFigure("Today", cost?.today)
                    costFigure("Last 30 days", cost?.month)
                }
                Text((cost?.rough == true
                      ? "Rough estimate at API-equivalent rates — Codex reports only total tokens."
                      : "Estimate at API-equivalent rates, not your actual subscription bill."))
                    .font(.system(size: 10)).foregroundStyle(.secondary)
            }
        }
    }

    private func costFigure(_ label: String, _ value: Double?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            Text(value.map { String(format: "$%.2f", $0) } ?? "—")
                .font(.system(size: 18, weight: .semibold))
        }
    }

    private func extraUsageCard(_ provider: ProviderPayload) -> some View {
        let extra = provider.extraUsage
        return card("Extra usage") {
            VStack(spacing: 12) {
                if let util = extra?.utilization {
                    UsageLimitBar(title: extra?.enabled == true ? "Pay-as-you-go used" : "Extra usage (off)",
                                  limit: LimitPayload(label: nil, usedPercent: util, windowMinutes: nil, resetsAt: nil),
                                  tint: accent)
                }
                if let spend = extra?.spendPercent {
                    UsageLimitBar(title: "Spend budget",
                                  limit: LimitPayload(label: nil, usedPercent: spend, windowMinutes: nil, resetsAt: nil),
                                  tint: accent)
                }
            }
        }
    }

    private func creditsCard(_ provider: ProviderPayload) -> some View {
        card("Credits") {
            HStack(spacing: 24) {
                if let balance = provider.credits?.balance {
                    creditFigure("Balance", provider.credits?.unlimited == true ? "Unlimited" : "\(Int(balance))")
                }
                if let resets = provider.freeResets {
                    creditFigure("Free resets", "\(resets)")
                }
            }
        }
    }

    private func creditFigure(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(label).font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary)
            Text(value).font(.system(size: 18, weight: .semibold))
        }
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

    // Headline "how close am I to a wall" view: the most-constrained limit as a ring with its reset
    // countdown and burn-rate projection, the next limit as a slim bar, and today's spend.
    @ViewBuilder
    private func atGlanceCard(_ provider: ProviderPayload) -> some View {
        if let primary = model.primaryLimit(provider), let percent = primary.usedPercent {
            let secondary = (provider.limits ?? []).first { $0.label != primary.label && $0.usedPercent != nil }
            let projection = model.projection(primary)
            card("At a glance") {
                HStack(alignment: .center, spacing: 16) {
                    HeroRing(percent: percent, caption: primary.label ?? "Limit")
                    VStack(alignment: .leading, spacing: 7) {
                        if let reset = resetRelative(primary.resetsAt) {
                            Label(reset, systemImage: "clock.arrow.circlepath")
                                .font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                        }
                        if let time = projection?.timeToLimit {
                            Label(time, systemImage: "chart.line.uptrend.xyaxis")
                                .font(.system(size: 11, weight: .semibold)).foregroundStyle(usageTint(percent))
                        } else {
                            Label("Well within limit", systemImage: "checkmark.seal")
                                .font(.system(size: 11, weight: .medium)).foregroundStyle(.secondary)
                        }
                        if let secondary, let spct = secondary.usedPercent {
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Text((secondary.label ?? "Limit").uppercased())
                                        .font(.system(size: 8.5, weight: .bold)).tracking(0.4).foregroundStyle(.secondary)
                                    Spacer()
                                    Text("\(Int(spct))%").font(.system(size: 10, weight: .semibold))
                                }
                                GeometryReader { geo in
                                    ZStack(alignment: .leading) {
                                        Capsule().fill(Color.primary.opacity(0.09)).frame(height: 5)
                                        Capsule().fill(usageTint(spct))
                                            .frame(width: max(5, geo.size.width * CGFloat(min(100, spct)) / 100), height: 5)
                                    }
                                }
                                .frame(height: 5)
                            }
                            .padding(.top, 1)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                }
                if let today = provider.cost?.today {
                    Divider().padding(.vertical, 2)
                    HStack {
                        Text("SPENT TODAY").font(.system(size: 8.5, weight: .bold)).tracking(0.5).foregroundStyle(.secondary)
                        Spacer()
                        Text(String(format: "$%.2f", today)).font(.system(size: 13, weight: .semibold))
                    }
                }
            }
        }
    }

    private func limitsCard(_ provider: ProviderPayload) -> some View {
        card("Limits") {
            if let limits = provider.limits, !limits.isEmpty {
                VStack(spacing: 12) {
                    ForEach(Array(limits.prefix(3).enumerated()), id: \.offset) { _, limit in
                        UsageLimitBar(title: limit.label ?? "Limit", limit: limit, tint: accent,
                                      projection: model.projection(limit)?.timeToLimit, autoTint: true)
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

    private func donutCard(_ title: String, pairs: [(String, Double)], emptyNote: String) -> some View {
        let palette: [Color] = [
            accent, Color(red: 0.28, green: 0.7, blue: 0.55), Color(red: 0.85, green: 0.6, blue: 0.25),
            Color(red: 0.6, green: 0.45, blue: 0.85), Color(red: 0.9, green: 0.42, blue: 0.42), Color.gray,
        ]
        let filtered = pairs.filter { $0.1 > 0 }
        var slices: [DonutSlice] = Array(filtered.prefix(5).enumerated()).map { index, pair in
            DonutSlice(label: pair.0, value: pair.1, color: palette[index % palette.count])
        }
        let remainder = filtered.dropFirst(5).reduce(0) { $0 + $1.1 }
        if remainder > 0 { slices.append(DonutSlice(label: "Other", value: remainder, color: palette[5])) }
        return card(title) {
            if slices.isEmpty {
                Text(emptyNote).font(.system(size: 12)).foregroundStyle(.secondary)
            } else {
                DonutChart(slices: slices)
            }
        }
    }

    private func modelsCard(_ provider: ProviderPayload) -> some View {
        donutCard("Models · 30 days",
                  pairs: (provider.models ?? []).map { (prettyModel($0.model), $0.tokens) },
                  emptyNote: "Per-model usage isn’t reported by \(MonitorModel.providerName(provider.provider)).")
    }

    private func platformsCard(_ provider: ProviderPayload) -> some View {
        donutCard("Where it runs · 30 days",
                  pairs: (provider.platforms ?? []).map { (prettyPlatform($0.platform), $0.tokens) },
                  emptyNote: "Per-platform usage isn’t reported by \(MonitorModel.providerName(provider.provider)).")
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

// MARK: - Live touchscreen (native reproduction of the KVM screen)

extension Color {
    init(hex: String) {
        let string = hex.hasPrefix("#") ? String(hex.dropFirst()) : hex
        var value: UInt64 = 0
        Scanner(string: string).scanHexInt64(&value)
        self.init(.sRGB, red: Double((value >> 16) & 0xff) / 255,
                  green: Double((value >> 8) & 0xff) / 255, blue: Double(value & 0xff) / 255, opacity: 1)
    }
}

struct ScreenTheme { let background, text, muted, line, bar, secondary: Color }

func screenTheme(_ id: String) -> ScreenTheme {
    switch id {
    case "codex": return ScreenTheme(background: Color(hex: "f5f5f2"), text: Color(hex: "101312"), muted: Color(hex: "68716e"), line: Color(hex: "cbd1ce"), bar: Color(hex: "10a37f"), secondary: Color(hex: "10a37f"))
    case "copilot": return ScreenTheme(background: Color(hex: "f6f8fa"), text: Color(hex: "24292f"), muted: Color(hex: "68717c"), line: Color(hex: "d0d7de"), bar: Color(hex: "8534f3"), secondary: Color(hex: "fe4c25"))
    case "gemini": return ScreenTheme(background: Color(hex: "f7f9fc"), text: Color(hex: "202124"), muted: Color(hex: "6c727b"), line: Color(hex: "d2d8e2"), bar: Color(hex: "4285f4"), secondary: Color(hex: "9168c0"))
    case "grok": return ScreenTheme(background: Color(hex: "050505"), text: Color(hex: "f7f7f7"), muted: Color(hex: "a0a0a0"), line: Color(hex: "353535"), bar: Color(hex: "f7f7f7"), secondary: Color(hex: "8d99a1"))
    default: return ScreenTheme(background: Color(hex: "0d1112"), text: Color(hex: "f4f5f2"), muted: Color(hex: "939b98"), line: Color(hex: "303637"), bar: Color(hex: "d97757"), secondary: Color(hex: "53d59c"))
    }
}

func providerBrand(_ id: String) -> (String, String) {
    ["claude": ("CLAUDE", "CODE"), "codex": ("CODEX", "OPENAI"), "copilot": ("COPILOT", "GITHUB"),
     "gemini": ("GEMINI", "CLI"), "grok": ("GROK", "BUILD")][id] ?? (id.uppercased(), "")
}

func providerLogoImage(_ id: String) -> NSImage? {
    guard let url = Bundle.main.resourceURL?.appendingPathComponent("providers/\(id).png") else { return nil }
    return NSImage(contentsOf: url)
}

private struct WorkingGlyph: View {
    let working: Bool
    let color: Color

    var body: some View {
        if working {
            TimelineView(.animation) { context in
                let time = context.date.timeIntervalSinceReferenceDate
                HStack(spacing: 1.6) {
                    ForEach(0..<4, id: \.self) { index in
                        Capsule().fill(color).frame(width: 2.2, height: 4 + 8 * abs(sin(time * 3 + Double(index) * 0.6)))
                    }
                }
                .frame(height: 12)
            }
        } else {
            Circle().fill(color).frame(width: 6, height: 6)
        }
    }
}

struct TouchscreenCard: View {
    let provider: ProviderPayload
    let working: Bool

    var body: some View {
        let theme = screenTheme(provider.provider)
        let brand = providerBrand(provider.provider)
        let today = (provider.daily ?? []).last?.totalTokens ?? 0
        let limits = Array((provider.limits ?? []).prefix(2))
        return VStack {
            screen(theme: theme, brand: brand, today: today, limits: limits)
                .padding(11)
                .frame(maxWidth: .infinity)
                .aspectRatio(3, contentMode: .fit)
                .background(theme.background)
                .clipShape(RoundedRectangle(cornerRadius: 11, style: .continuous))
        }
        .padding(11)
        .background(LinearGradient(colors: [Color(white: 0.2), Color(white: 0.08)], startPoint: .top, endPoint: .bottom))
        .clipShape(RoundedRectangle(cornerRadius: 18, style: .continuous))
        .shadow(color: .black.opacity(0.3), radius: 8, y: 3)
    }

    private func screen(theme: ScreenTheme, brand: (String, String), today: Double, limits: [LimitPayload]) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 8) {
                Group {
                    if let image = providerLogoImage(provider.provider) {
                        Image(nsImage: image).resizable().scaledToFit()
                    } else {
                        RoundedRectangle(cornerRadius: 5).fill(theme.bar)
                    }
                }
                .frame(width: 22, height: 22)
                VStack(alignment: .leading, spacing: 0) {
                    Text(brand.0).font(.system(size: 12, weight: .heavy)).foregroundStyle(theme.text)
                    Text(brand.1).font(.system(size: 8, weight: .semibold)).tracking(0.5).foregroundStyle(theme.muted)
                }
                Spacer()
                HStack(spacing: 5) {
                    WorkingGlyph(working: working, color: working ? theme.secondary : theme.muted)
                    Text(working ? "WORK" : "READY").font(.system(size: 9, weight: .bold)).foregroundStyle(working ? theme.secondary : theme.muted)
                }
                .padding(.horizontal, 8).padding(.vertical, 4)
                .background((working ? theme.secondary : theme.muted).opacity(0.16), in: Capsule())
            }
            Spacer(minLength: 2)
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 1) {
                    Text("TODAY").font(.system(size: 8, weight: .semibold)).tracking(0.5).foregroundStyle(theme.muted)
                    Text(tokenShort(today)).font(.system(size: 20, weight: .heavy)).foregroundStyle(theme.text)
                }
                Rectangle().fill(theme.line).frame(width: 1).frame(maxHeight: .infinity)
                VStack(spacing: 7) {
                    if limits.isEmpty {
                        Text("Waiting for usage").font(.system(size: 9)).foregroundStyle(theme.muted).frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        ForEach(Array(limits.enumerated()), id: \.offset) { _, limit in
                            miniLimit(limit, theme: theme)
                        }
                    }
                }
                .frame(maxWidth: .infinity)
            }
        }
    }

    private func miniLimit(_ limit: LimitPayload, theme: ScreenTheme) -> some View {
        let percent = limit.usedPercent ?? 0
        return VStack(alignment: .leading, spacing: 3) {
            HStack {
                Text((limit.label ?? "Limit").uppercased()).font(.system(size: 7, weight: .semibold)).tracking(0.3).foregroundStyle(theme.muted)
                Spacer()
                Text(limit.usedPercent == nil ? "--" : "\(Int(percent))%").font(.system(size: 8, weight: .semibold)).foregroundStyle(theme.text)
            }
            GeometryReader { geo in
                ZStack(alignment: .leading) {
                    Capsule().fill(theme.line).frame(height: 5)
                    Capsule().fill(theme.bar).frame(width: max(5, geo.size.width * CGFloat(min(100, percent)) / 100), height: 5)
                }
            }
            .frame(height: 5)
        }
    }
}

// Each menu-bar metric gets a small glyph built from two NSImages — a template track (adopts the
// menu-bar/panel color) under a colored fill — because a SwiftUI Canvas draws nothing in a
// MenuBarExtra label, while Image/Text render reliably.

// Battery draining with remaining budget. In the menu bar the fill renders `.template` (clean
// monochrome — the menu bar flattens colors anyway); the Settings preview passes colored: true for
// the full band color. (Usage gauge mode.)
struct GaugeIconView: View {
    let fillPercent: Double?
    let colorPercent: Double?
    var colored: Bool = false
    var body: some View {
        ZStack {
            Image(nsImage: gaugeFrameIcon())
            if let fillPercent, let colorPercent {
                Image(nsImage: gaugeFillIcon(fillPercent: fillPercent, colorPercent: colorPercent))
                    .renderingMode(colored ? .original : .template)
            }
        }
        .frame(width: 20, height: 14)
    }
}

// Circular ring showing remaining budget. (Limit % mode.)
struct RingGaugeView: View {
    let fillPercent: Double?
    let colorPercent: Double?
    var colored: Bool = false
    var body: some View {
        ZStack {
            Image(nsImage: ringTrackIcon())
            if let fillPercent, let colorPercent {
                Image(nsImage: ringArcIcon(fillPercent: fillPercent, colorPercent: colorPercent))
                    .renderingMode(colored ? .original : .template)
            }
        }
        .frame(width: 15, height: 14)
    }
}

// Two bars (session, weekly), each showing remaining budget and colored by its own usage.
struct DualBarsView: View {
    let sessionFill: Double?
    let sessionColor: Double?
    let weeklyFill: Double?
    let weeklyColor: Double?
    var colored: Bool = false
    var body: some View {
        ZStack {
            Image(nsImage: dualTrackIcon())
            Image(nsImage: dualFillIcon(sessionFill: sessionFill, sessionColor: sessionColor,
                                        weeklyFill: weeklyFill, weeklyColor: weeklyColor))
                .renderingMode(colored ? .original : .template)
        }
        .frame(width: 14, height: 14)
    }
}

// The full menu-bar representation for the current mode. Shared by the live MenuBarExtra label and
// the Settings preview so the two never drift.
struct MenuBarContent: View {
    @ObservedObject var model: MonitorModel
    // Menu bar flattens colors to monochrome, so the live label stays mono; the Settings preview
    // (colored: true) shows the real band color and colored numbers.
    var colored: Bool = false

    private func numeral(_ text: String, _ tint: Color) -> some View {
        Text(text).font(.system(size: 12, weight: .semibold)).monospacedDigit()
            .foregroundStyle(colored ? tint : .primary)
    }

    var body: some View {
        if model.menuBarMode == .icon {
            Image(nsImage: statusBarIcon(healthy: model.isHealthy))
        } else if model.menuBarMode == .gauge {
            let summary = model.menuBarSummary
            HStack(spacing: 4) {
                GaugeIconView(fillPercent: summary?.fillPercent, colorPercent: summary?.colorPercent, colored: colored)
                if summary?.fillPercent != nil { numeral(summary?.text ?? "", summary?.tint ?? .primary) }
            }
        } else if let summary = model.menuBarSummary {
            switch model.menuBarMode {
            case .limitPercent:
                HStack(spacing: 4) {
                    RingGaugeView(fillPercent: summary.fillPercent, colorPercent: summary.colorPercent, colored: colored)
                    numeral(summary.text, summary.tint)
                }
            case .dual:
                HStack(spacing: 4) {
                    DualBarsView(sessionFill: summary.fillPercent, sessionColor: summary.colorPercent,
                                 weeklyFill: summary.secondaryFill, weeklyColor: summary.secondaryColor, colored: colored)
                    numeral(summary.secondary.map { "\(summary.text) · \($0)" } ?? summary.text, summary.tint)
                }
            case .costToday:
                HStack(spacing: 3) {
                    Image(systemName: "dollarsign.circle.fill").font(.system(size: 11)).foregroundStyle(.secondary)
                    numeral(summary.text, summary.tint)
                }
            default:
                numeral(summary.text, summary.tint)
            }
        } else {
            Image(nsImage: statusBarIcon(healthy: model.isHealthy))
        }
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
            MenuBarContent(model: model)
        }
        .menuBarExtraStyle(.window)
    }
}
#endif
