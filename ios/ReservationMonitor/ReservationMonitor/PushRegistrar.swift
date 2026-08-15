import Foundation
import UIKit
import UserNotifications

/// Tracks where we are in the APNs registration handshake so the UI can show
/// the device token (which has to be copied into the GitHub Actions secret) or
/// explain what went wrong.
@MainActor
@Observable
final class PushRegistrar {
    enum State: Equatable {
        case idle
        case requestingPermission
        case permissionDenied
        case registering
        case registered(token: String)
        case failed(message: String)
    }

    private(set) var state: State = .idle

    /// The token is stashed so a relaunch shows it immediately, before APNs
    /// has had a chance to call back.
    private static let tokenKey = "apnsDeviceToken"

    init() {
        if let saved = UserDefaults.standard.string(forKey: Self.tokenKey) {
            state = .registered(token: saved)
        }
    }

    var deviceToken: String? {
        if case let .registered(token) = state { return token }
        return nil
    }

    func requestAuthorization() async {
        if deviceToken == nil { state = .requestingPermission }
        do {
            let granted = try await UNUserNotificationCenter.current()
                .requestAuthorization(options: [.alert, .sound, .badge])
            guard granted else {
                state = .permissionDenied
                return
            }
            if deviceToken == nil { state = .registering }
            UIApplication.shared.registerForRemoteNotifications()
        } catch {
            state = .failed(message: error.localizedDescription)
        }
    }

    func didRegister(deviceToken data: Data) {
        let token = data.map { String(format: "%02x", $0) }.joined()
        UserDefaults.standard.set(token, forKey: Self.tokenKey)
        state = .registered(token: token)
    }

    func didFailToRegister(error: Error) {
        state = .failed(message: error.localizedDescription)
    }
}
