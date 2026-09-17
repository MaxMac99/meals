import SwiftUI

/// The built-in assistant: a plain chat over the server's agent. The look is
/// the app's, not a messaging app's — system typography, the shared accent,
/// no custom bubbles theme. Everything behavioural lives in
/// `AssistantStore`; this view only renders state and asks for input.
struct AssistantView: View {
    @Environment(Session.self) private var session
    @Environment(AssistantStore.self) private var store

    @State private var draft = ""
    @FocusState private var inputFocused: Bool

    var body: some View {
        NavigationStack {
            Group {
                if session.assistantEnabled {
                    conversation
                } else {
                    disabledView
                }
            }
            .navigationTitle("Assistant")
            .navigationBarTitleDisplayMode(.inline)
        }
    }

    // MARK: disabled

    private var disabledView: some View {
        ContentUnavailableView {
            Label("No AI assistant", systemImage: "sparkles.slash")
        } description: {
            Text(
                "This server doesn't have the AI assistant configured. Ask whoever runs it to set "
                    + "LLM_PROVIDER, OPENAI_API_KEY and OPENAI_MODEL — everything else in Meals works as usual."
            )
        }
    }

    // MARK: conversation

    private var conversation: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 12) {
                    if store.hasConversation {
                        ForEach(store.messages) { message in
                            Bubble(message: message)
                                .id(message.id)
                        }
                        if !store.references.isEmpty {
                            ReferenceRow(references: store.references)
                        }
                    } else {
                        EmptyState { prompt in
                            Task { await store.send(prompt) }
                        }
                    }
                    if store.state == .sending {
                        HStack(spacing: 8) {
                            ProgressView()
                            Text("Thinking…").foregroundStyle(.secondary)
                        }
                        .padding(.horizontal)
                        .id("pending")
                    }
                }
                .padding(.vertical, 8)
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: store.messages.count) {
                withAnimation {
                    if let last = store.messages.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    } else {
                        proxy.scrollTo("pending", anchor: .bottom)
                    }
                }
            }
            .safeAreaInset(edge: .bottom) { bottomBar }
            .confirmationDialog(
                store.pendingAction?.summary ?? "",
                isPresented: Binding(
                    get: { store.pendingAction != nil },
                    set: { if !$0 { store.denyPendingAction() } }
                ),
                titleVisibility: .visible
            ) {
                Button("Run it", role: .destructive) {
                    Task { await store.confirmPendingAction() }
                }
                Button("Not now", role: .cancel) {
                    store.denyPendingAction()
                }
            } message: {
                Text("This can't be undone from the chat.")
            }
        }
    }

    // MARK: bottom bar

    private var bottomBar: some View {
        VStack(spacing: 8) {
            if case .failed(let reason) = store.state {
                HStack(alignment: .firstTextBaseline) {
                    Text(reason)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("Retry") {
                        Task { await store.retry() }
                    }
                }
                .padding(.horizontal)
            }
            HStack(spacing: 8) {
                TextField("Ask about meals, plan, shopping…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...4)
                    .focused($inputFocused)
                    .disabled(!store.canSend)
                Button {
                    let text = draft
                    draft = ""
                    Task { await store.send(text) }
                } label: {
                    Image(systemName: "arrow.up.circle.fill")
                        .font(.title2)
                }
                .disabled(!store.canSend || draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
        }
        .background(.bar)
    }
}

/// One message: the user's on the right in the accent, the assistant's on
/// the left in a secondary fill — the platform's own shape, no custom theme.
private struct Bubble: View {
    let message: AssistantMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 48) }
            Text(message.content)
                .textSelection(.enabled)
                .padding(.vertical, 8)
                .padding(.horizontal, 12)
                .background(message.role == .user ? Color.accentColor.opacity(0.15) : Color(.systemFill))
                .clipShape(RoundedRectangle(cornerRadius: 16))
            if message.role == .assistant { Spacer(minLength: 48) }
        }
        .padding(.horizontal)
    }
}

/// What the answer acted on. Read-only for now — tapping through to the
/// matching screen comes with the next navigation pass.
private struct ReferenceRow: View {
    let references: [AssistantReference]

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(references) { reference in
                    Label(reference.title ?? reference.type, systemImage: icon(for: reference.type))
                        .font(.caption)
                        .padding(.vertical, 4)
                        .padding(.horizontal, 10)
                        .background(Color(.systemFill), in: Capsule())
                }
            }
            .padding(.horizontal)
        }
    }

    private func icon(for type: String) -> String {
        switch type {
        case "recipe": "book"
        case "meal": "fork.knife"
        case "plan", "plan_meal": "list.bullet.rectangle"
        case "list_item": "cart"
        case "freezer_item": "snowflake"
        case "ingredient": "carrot"
        case "supermarket": "storefront"
        default: "sparkles"
        }
    }
}

/// The empty chat: what this is for, and the questions that start it.
private struct EmptyState: View {
    let send: (String) -> Void
    @Environment(AssistantStore.self) private var store

    var body: some View {
        VStack(spacing: 16) {
            Image(systemName: "sparkles")
                .font(.largeTitle)
                .foregroundStyle(.tint)
            Text("Ask about your meals, your plan or your shopping list.")
                .multilineTextAlignment(.center)
                .foregroundStyle(.secondary)
            VStack(spacing: 8) {
                ForEach(store.suggestedPrompts, id: \.self) { prompt in
                    Button {
                        send(prompt)
                    } label: {
                        Text(prompt)
                            .frame(maxWidth: .infinity)
                    }
                    .buttonStyle(.bordered)
                }
            }
            .padding(.horizontal, 24)
        }
        .padding(.top, 48)
        .frame(maxWidth: .infinity)
    }
}
