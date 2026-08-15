import SwiftUI

@main
struct ReservationMonitorApp: App {
    @UIApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate

    var body: some Scene {
        WindowGroup {
            ContentView(registrar: appDelegate.registrar)
        }
    }
}
