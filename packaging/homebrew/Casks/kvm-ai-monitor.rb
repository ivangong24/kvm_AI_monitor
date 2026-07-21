cask "kvm-ai-monitor" do
  version "0.7.0"
  sha256 :no_check

  url "https://github.com/ivangong24/kvm_AI_monitor/releases/download/v#{version}/KVM-AI-Monitor-v#{version}.zip"
  name "KVM AI Monitor"
  desc "Menu bar companion for the GL.iNet Comet Pro AI usage dashboard"
  homepage "https://github.com/ivangong24/kvm_AI_monitor"

  depends_on macos: ">= :ventura"

  app "KVM AI Monitor.app"

  zap trash: [
    "~/Library/Preferences/com.kvm-ai-monitor.menubar.plist",
    "~/Library/Saved Application State/com.kvm-ai-monitor.menubar.savedState",
  ]
end
