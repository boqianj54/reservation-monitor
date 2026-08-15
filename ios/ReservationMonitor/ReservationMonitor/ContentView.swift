import SwiftUI

/// The app has no day-to-day job beyond receiving pushes, so the whole UI is a
/// status readout plus the device token that has to be pasted into the
/// APNS_DEVICE_TOKENS GitHub Actions secret.
struct ContentView: View {
    @Bindable var registrar: PushRegistrar
    @State private var copied = false

    var body: some View {
        NavigationStack {
            List {
                Section("Status") {
                    statusRow
                }

                if let token = registrar.deviceToken {
                    Section {
                        Text(token)
                            .font(.system(.footnote, design: .monospaced))
                            .textSelection(.enabled)
                        Button {
                            UIPasteboard.general.string = token
                            copied = true
                        } label: {
                            Label(copied ? "Copied" : "Copy device token",
                                  systemImage: copied ? "checkmark" : "doc.on.doc")
                        }
                        ShareLink(item: token) {
                            Label("Share", systemImage: "square.and.arrow.up")
                        }
                    } header: {
                        Text("Device token")
                    } footer: {
                        Text("Paste this into the APNS_DEVICE_TOKENS secret in the "
                             + "reservation-monitor GitHub repository.")
                    }
                }

                Section {
                    Text("This app only listens. The checker runs in GitHub Actions "
                         + "every 5 minutes and pushes here the moment a bookable "
                         + "table appears in your date range.")
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                }
            }
            .navigationTitle("Reservation Monitor")
        }
        .task { await registrar.requestAuthorization() }
    }

    @ViewBuilder
    private var statusRow: some View {
        switch registrar.state {
        case .idle, .requestingPermission:
            Label("Asking for notification permission…", systemImage: "clock")
        case .registering:
            Label("Registering with Apple…", systemImage: "clock")
        case .registered:
            Label("Ready to receive alerts", systemImage: "checkmark.circle.fill")
                .foregroundStyle(.green)
        case .permissionDenied:
            Label("Notifications are turned off. Enable them in Settings.",
                  systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
        case let .failed(message):
            Label("Registration failed: \(message)",
                  systemImage: "xmark.octagon.fill")
                .foregroundStyle(.red)
        }
    }
}
