import XCTest
@testable import Meals

/// The assistant's client half: response decoding and the store's send /
/// confirm / retry / failure behaviour, over the same URLProtocol stub the
/// other API tests use. The server is scripted, never real.
/// A mutable recording box: the stub handler is `@Sendable`, so captured
/// vars are off limits — a class reference keeps the counters honest under
/// Swift 6 strict concurrency.
private final class Recorder: @unchecked Sendable {
    var bodies: [Data] = []
    var callCount = 0
}

final class AssistantTests: XCTestCase {
    private func makeClient() -> APIClient {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return APIClient(
            baseURL: URL(string: "http://testserver")!,
            token: "meals_test-token",
            session: URLSession(configuration: config)
        )
    }

    @MainActor
    private func makeStore() -> AssistantStore {
        AssistantStore(api: { self.makeClient() })
    }

    private static let answerBody = Data(
        (#"{"message": {"role": "assistant", "content": "Spag bol is on the plan."}, "# +
            #""references": [{"type": "meal", "id": "11111111-1111-1111-1111-111111111111", "title": "Spag bol"}], "action": null}"#)
            .utf8
    )

    private static let confirmationBody = Data(
        (#"{"message": {"role": "assistant", "content": "Delete the recipe 'Bad parse' from the library permanently?"}, "# +
            #""references": [], "action": {"tool": "delete_recipe", "arguments": {"title": "Bad parse"}, "# +
            #""summary": "Delete the recipe 'Bad parse' from the library permanently?"}}"#)
            .utf8
    )

    // MARK: decoding

    func testAssistantResponseDecodes() throws {
        let decoder = APIClient.decoder()
        let response = try decoder.decode(AssistantChatResponse.self, from: Self.answerBody)
        XCTAssertEqual(response.message.content, "Spag bol is on the plan.")
        XCTAssertEqual(response.references?.first?.type, "meal")
        XCTAssertEqual(response.references?.first?.title, "Spag bol")
        XCTAssertNil(response.action)
    }

    func testAssistantResponseWithoutOptionalPartsDecodes() throws {
        // A future server may omit references or the action entirely; a chat
        // that still answers must not fail on a missing list.
        let data = Data(#"{"message": {"role": "assistant", "content": "hi"}}"#.utf8)
        let response = try APIClient.decoder().decode(AssistantChatResponse.self, from: data)
        XCTAssertEqual(response.message.content, "hi")
        XCTAssertNil(response.references)
        XCTAssertNil(response.action)
    }

    func testActionArgumentsRoundTripThroughJSONValue() throws {
        let body = Data(
            (#"{"tool": "delete_meal", "arguments": {"meal_name": "Curry", "portions": 2, "all_of_it": true, "# +
                #""nested": {"a": [1, "b"]}, "note": null}, "summary": "s"}"#).utf8
        )
        let action = try APIClient.decoder().decode(AssistantAction.self, from: body)
        XCTAssertEqual(action.tool, "delete_meal")
        XCTAssertEqual(action.arguments["meal_name"], JSONValue.string("Curry"))
        XCTAssertEqual(action.arguments["portions"], JSONValue.number(2))
        XCTAssertEqual(action.arguments["all_of_it"], JSONValue.bool(true))
        XCTAssertEqual(
            action.arguments["nested"],
            JSONValue.object(["a": JSONValue.array([JSONValue.number(1), JSONValue.string("b")])])
        )
        XCTAssertTrue(action.arguments["note"] == JSONValue.null)
        // And back to wire shapes without loss — the confirmed action must
        // reach the server exactly as it came.
        let any = action.arguments.mapValues { $0.anyValue }
        XCTAssertEqual(any["meal_name"] as? String, "Curry")
        XCTAssertEqual(any["portions"] as? Double, 2)
        XCTAssertEqual((any["all_of_it"] as? NSNumber).map { CFGetTypeID($0) == CFBooleanGetTypeID() }, true)
        XCTAssertTrue(any["note"] is NSNull)
    }

    func testClientConfigCarriesAssistantEnabled() throws {
        let with = try APIClient.decoder().decode(
            ClientConfig.self, from: Data(#"{"api_version": "1", "min_ios_build": 0, "current_ios_build": 1, "assistant_enabled": true}"#.utf8)
        )
        XCTAssertEqual(with.assistantEnabled, true)
        let without = try APIClient.decoder().decode(
            ClientConfig.self, from: Data(#"{"api_version": "1", "min_ios_build": 0, "current_ios_build": 1}"#.utf8)
        )
        // Absent means an older server: no assistant, and the tab shows it.
        XCTAssertNil(without.assistantEnabled)
    }

    // MARK: the store

    @MainActor
    func testSendAppendsUserAndAssistantAndKeepsReferences() async {
        StubProtocol.handler = { request in
            XCTAssertEqual(request.url?.path, "/assistant/chat")
            return (200, Self.answerBody)
        }
        let store = makeStore()
        await store.send("Was gibt es heute zum Kochen?")
        XCTAssertEqual(store.messages.count, 2)
        XCTAssertEqual(store.messages[0].role, .user)
        XCTAssertEqual(store.messages[0].content, "Was gibt es heute zum Kochen?")
        XCTAssertEqual(store.messages[1].role, .assistant)
        XCTAssertEqual(store.messages[1].content, "Spag bol is on the plan.")
        XCTAssertEqual(store.references.first?.title, "Spag bol")
        XCTAssertEqual(store.state, .idle)
        XCTAssertNil(store.pendingAction)
    }

    @MainActor
    func testBlankInputIsNotSent() async {
        StubProtocol.handler = { _ in (200, Self.answerBody) }
        let store = makeStore()
        await store.send("   ")
        XCTAssertEqual(store.messages, [])
    }

    @MainActor
    func testFailureKeepsTheQuestionAndOffersRetry() async {
        StubProtocol.handler = { _ in
            (502, Data(#"{"detail": "the LLM provider could not be reached"}"#.utf8))
        }
        let store = makeStore()
        await store.send("Was soll ich kochen?")
        XCTAssertEqual(store.messages.count, 1, "the user's question stays for the retry")
        guard case .failed(let reason) = store.state else {
            return XCTFail("expected a failure state, got \(store.state)")
        }
        XCTAssertTrue(reason.contains("could not be reached"))

        StubProtocol.handler = { _ in (200, Self.answerBody) }
        await store.retry()
        XCTAssertEqual(store.messages.count, 2)
        XCTAssertEqual(store.state, .idle)
    }

    @MainActor
    func testConfirmationHoldsTheActionAndSendsItVerbatim() async throws {
        let recorder = Recorder()
        StubProtocol.handler = { request in
            recorder.bodies.append(request.streamedBody() ?? Data())
            recorder.callCount += 1
            return (200, recorder.callCount == 1 ? Self.confirmationBody : Self.answerBody)
        }
        let store = makeStore()
        await store.send("delete the bad parse")
        guard let action = store.pendingAction else { return XCTFail("expected a held action") }
        XCTAssertEqual(action.tool, "delete_recipe")
        XCTAssertEqual(action.arguments["title"], .string("Bad parse"))
        XCTAssertEqual(store.messages.count, 2, "the ask text is the answer so far")

        await store.confirmPendingAction()
        XCTAssertEqual(store.messages.count, 3)
        XCTAssertEqual(store.messages.last?.content, "Spag bol is on the plan.")
        XCTAssertNil(store.pendingAction)
        let body = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(recorder.bodies.last)) as? [String: Any]
        )
        let confirmed = try XCTUnwrap(body["confirmed_action"] as? [String: Any])
        XCTAssertEqual(confirmed["tool"] as? String, "delete_recipe")
        let arguments = try XCTUnwrap(confirmed["arguments"] as? [String: Any])
        XCTAssertEqual(arguments["title"] as? String, "Bad parse")
    }

    @MainActor
    func testDenialRunsNothingAndSaysSo() async {
        let recorder = Recorder()
        StubProtocol.handler = { _ in
            recorder.callCount += 1
            return (200, Self.confirmationBody)
        }
        let store = makeStore()
        await store.send("delete the bad parse")
        XCTAssertEqual(recorder.callCount, 1)
        store.denyPendingAction()
        XCTAssertNil(store.pendingAction)
        XCTAssertEqual(store.messages.count, 3)
        XCTAssertEqual(store.messages.last?.content, "Okay — nichts geändert.")
        await store.confirmPendingAction() // nothing held any more
        XCTAssertEqual(recorder.callCount, 1, "a denial must not send a second request")
    }

    @MainActor
    func testSuggestedPromptsExistForAnEmptyChat() {
        let store = makeStore()
        XCTAssertFalse(store.suggestedPrompts.isEmpty)
        XCTAssertTrue(store.suggestedPrompts.allSatisfy { !$0.isEmpty })
    }
}
