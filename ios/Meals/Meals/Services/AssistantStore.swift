import Foundation
import Observation

/// The chat: the conversation the app holds, the send/confirm states, and
/// the retry. No disk persistence on purpose — the transcript is a working
/// scratchpad, and the server holds the real state the tools acted on.
///
/// Confirmation is the one subtlety. A destructive action comes back as an
/// ``AssistantAction`` and the view raises a native dialog from it; on a
/// yes the same object goes back verbatim as `confirmed_action`, and the
/// server re-validates it before running anything. A retry after a failed
/// confirmation may re-run an action that already went through — the server
/// answers with its own refusal sentence (e.g. already gone) and nothing is
/// lost twice.
@MainActor
@Observable
final class AssistantStore {
    enum SendState: Equatable {
        case idle
        case sending
        case failed(String)
    }

    private(set) var messages: [AssistantMessage] = []
    /// What the latest answer is about — the hook for navigation chips.
    private(set) var references: [AssistantReference] = []
    private(set) var state: SendState = .idle
    /// Set while a destructive action waits for the household's yes.
    private(set) var pendingAction: AssistantAction?

    /// The empty-chat suggestions, in the order a household asks.
    let suggestedPrompts = [
        "Was soll ich heute kochen?",
        "Was muss ich einkaufen?",
        "Plane meine nächsten Mahlzeiten.",
        "Welche Rezepte hatte ich länger nicht?",
    ]

    private let api: () -> APIClient

    init(api: @escaping () -> APIClient) {
        self.api = api
    }

    var canSend: Bool { state != .sending }
    var hasConversation: Bool { !messages.isEmpty }

    /// Send a new user message. The text is appended up front — the UI shows
    /// it immediately and a failure keeps it in place for the retry.
    func send(_ text: String) async {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, canSend else { return }
        messages.append(AssistantMessage(role: .user, content: trimmed))
        await dispatch(confirmedAction: nil)
    }

    /// The household said yes: run the held action, arguments untouched.
    func confirmPendingAction() async {
        guard let action = pendingAction, canSend else { return }
        pendingAction = nil
        await dispatch(confirmedAction: action)
    }

    /// The household said no: nothing ran, and the transcript says so.
    func denyPendingAction() {
        guard pendingAction != nil else { return }
        pendingAction = nil
        messages.append(AssistantMessage(role: .assistant, content: "Okay — nichts geändert."))
    }

    /// After a failure: resend exactly what was in flight — the last user
    /// message, or the confirmation that never completed.
    func retry() async {
        guard state != .sending else { return }
        state = .idle
        await dispatch(confirmedAction: nil)
    }

    private func dispatch(confirmedAction: AssistantAction?) async {
        state = .sending
        do {
            // The server trims to its own cap as well; keeping the client's
            // payload bounded is politeness, not correctness.
            let recent = Array(messages.suffix(24))
            let response = try await api().assistantChat(messages: recent, confirmedAction: confirmedAction)
            messages.append(AssistantMessage(role: .assistant, content: response.message.content))
            references = response.references ?? []
            if let action = response.action {
                pendingAction = action
            }
            state = .idle
        } catch {
            state = .failed(error.localizedDescription)
        }
    }
}
