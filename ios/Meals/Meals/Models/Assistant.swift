import Foundation

/// A JSON value that survives the round trip. The server's confirmation
/// ``AssistantAction/arguments`` come back from the API and must go back
/// verbatim on confirm, whatever they contain — Swift's `[String: Any]`
/// can't be `Codable` or `Sendable`, so this is what rides instead.
enum JSONValue: Codable, Equatable, Hashable, Sendable {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null
    case array([JSONValue])
    case object([String: JSONValue])

    init(_ any: Any) {
        switch any {
        case Optional<Any>.none:
            self = .null
        case let value as String:
            self = .string(value)
        case let value as NSNumber:
            // JSONSerialization hands back NSNumber for true/false too;
            // only the CF type tells a boolean from a number.
            self = CFGetTypeID(value) == CFBooleanGetTypeID() ? .bool(value.boolValue) : .number(value.doubleValue)
        case let value as [Any]:
            self = .array(value.map(JSONValue.init))
        case let value as [String: Any]:
            self = .object(value.mapValues(JSONValue.init))
        default:
            self = .null
        }
    }

    /// The Any shape `APIClient`'s JSONSerialization payloads want.
    var anyValue: Any {
        switch self {
        case .string(let value): value
        case .number(let value): value
        case .bool(let value): value
        case .null: NSNull()
        case .array(let values): values.map(\.anyValue)
        case .object(let values): values.mapValues(\.anyValue)
        }
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
        } else {
            throw DecodingError.dataCorruptedError(
                in: container, debugDescription: "unsupported JSON value"
            )
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .bool(let value): try container.encode(value)
        case .null: try container.encodeNil()
        case .array(let values): try container.encode(values)
        case .object(let values): try container.encode(values)
        }
    }
}

/// One message in the conversation the app holds. The `id` is local only —
/// it keeps list rows stable across reloads and never goes on the wire.
struct AssistantMessage: Equatable, Sendable, Identifiable {
    enum Role: String, Sendable {
        case user
        case assistant
    }

    let id: UUID
    let role: Role
    let content: String

    init(role: Role, content: String) {
        self.id = UUID()
        self.role = role
        self.content = content
    }
}

/// What the server sent back for its half of the conversation.
struct AssistantReply: Codable, Equatable, Sendable {
    let role: String
    let content: String
}

/// A destructive action waiting for the household's yes. `arguments` ride
/// as `JSONValue` so they can be posted back exactly as they came — the
/// server re-validates the envelope rather than trusting the client.
struct AssistantAction: Codable, Equatable, Sendable {
    let tool: String
    let arguments: [String: JSONValue]
    let summary: String
}

/// A concrete Meal object the answer is about — the hook for navigating to
/// the matching screen later.
struct AssistantReference: Codable, Equatable, Sendable, Identifiable {
    let type: String
    let id: UUID
    let title: String?
}

/// One assistant turn. `references` and `action` are optional in the decode
/// on purpose: a future server may omit either, and a chat that still
/// answers must never fail on a missing list.
struct AssistantChatResponse: Codable, Equatable, Sendable {
    let message: AssistantReply
    let references: [AssistantReference]?
    let action: AssistantAction?
}
