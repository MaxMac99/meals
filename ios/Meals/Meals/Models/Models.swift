import Foundation

/// Server slot vocabulary (dinner/lunch/breakfast/other) shown as a section
/// header or a caption. Deliberately a helper and not a String extension: an
/// API vocabulary shouldn't hang off every string in the app. The raw value
/// stays what is sent to the API.
enum SlotLabel {
    static func label(for slot: String) -> String {
        switch slot {
        case "dinner": String(localized: "dinner-slot")
        case "lunch": String(localized: "lunch-slot")
        case "breakfast": String(localized: "breakfast-slot")
        case "other": String(localized: "other-slot")
        default: slot.capitalized
        }
    }
}

// DTOs mirroring the Meals API responses (snake_case JSON decoded with
// .convertFromSnakeCase). Timestamps decode as strings, so a server that
// changes its date format can never turn a working screen into a decode
// failure; `TimestampLabel` is the one place that turns them into Dates,
// where something is shown or compared.

struct UserProfile: Codable, Equatable, Sendable {
    let id: UUID
    let email: String
    let displayName: String
    /// The household this account is in (Q19: a server holds many). Optional
    /// because a build can outlive the server it talks to, and a server from
    /// before Q19 sends neither — a missing name must not be a decode error.
    let householdId: UUID?
    let householdName: String?
    /// Which member leads the household (Q23): the account it is billed to, and
    /// the only one who may invite or remove people. Optional for the same
    /// reason as the two above — a server from before Q23 doesn't send it, and
    /// a build must not decode-fail against one.
    let householdLeadUserId: UUID?

    /// Whether to offer this account the things only a lead can do.
    ///
    /// **True when the server didn't say**, which looks like the wrong way
    /// round and isn't. A Q23 server always names a lead, so a missing field
    /// means the server predates the lead entirely — and on that server every
    /// member really can invite, exactly as Q19 had it. Hiding the button there
    /// would break a working feature against a server that was going to allow
    /// it. The server is the one enforcing this either way; this only decides
    /// whether to offer the control or let it be refused.
    var leadsHousehold: Bool { householdLeadUserId == nil || householdLeadUserId == id }
}

/// A household and everyone in it (`GET /auth/household`). Every member can
/// read this; only the lead can change who is in it (Q23).
struct Household: Codable, Equatable, Sendable {
    let id: UUID
    let name: String
    let leadUserId: UUID?
    let members: [HouseholdMember]
}

struct HouseholdMember: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let displayName: String
    let email: String
    let createdAt: String
    let isLead: Bool
    /// Who admitted them, from the invite they redeemed. Nil for whoever
    /// started the household, and nil once their inviter deletes their account.
    let invitedByUserId: UUID?
}

/// Result of `DELETE /auth/household/members/{id}`. `youLeft` is the difference
/// between "they are gone" and "you are" — when it's true the app is looking at
/// a household it is no longer in and has to reload everything.
struct MemberRemoved: Codable, Sendable {
    let removedUserId: UUID
    let youLeft: Bool
    let detail: String
}

/// A single-use code that lets one more person register into this household
/// (`POST /auth/invites`). `code` comes back exactly once and is never
/// recoverable — the server stores only its hash.
struct InviteCreated: Codable, Sendable {
    let id: UUID
    let code: String
    /// A string, like every other timestamp here — the decoder has no date
    /// strategy on purpose, so a server that changes its date format can't
    /// turn a working screen into a decode failure.
    let expiresAt: String

    /// "2 August 2026", or nil if the timestamp isn't one we recognise — in
    /// which case the sheet says the code is single-use and leaves it there.
    /// The words and punctuation come from the device's locale, via the
    /// date formatter in `TimestampLabel` ("2 August 2026" at home, "2.
    /// August 2026" in German).
    var expiryLabel: String? {
        TimestampLabel.long(expiresAt)
    }
}

/// An invite on the books (`GET /auth/invites`): status only — the code
/// itself is hashed server-side the moment it's minted, so a list can never
/// leak one.
struct InviteInfo: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let createdAt: String
    let expiresAt: String
    let acceptedAt: String?

    enum Status: Equatable {
        case open
        case redeemed
        case expired
    }

    /// `now` is injectable for tests; expiry is a real date comparison —
    /// parsed once by `TimestampLabel`, never compared by string prefix.
    /// An expiry the parser can't read stays open: a code the app can't
    /// time-stamp shouldn't be told it has died.
    func status(now: Date = .now) -> Status {
        if acceptedAt != nil { return .redeemed }
        guard let expiry = TimestampLabel.date(expiresAt) else { return .open }
        return expiry <= now ? .expired : .open
    }
}

/// A personal API token's metadata (`GET /auth/tokens`). Only creation ever
/// returns the token string itself, and only once.
struct APIToken: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let label: String?
    let createdAt: String
    let expiresAt: String?
    let lastUsedAt: String?
}

/// `POST /auth/tokens` — carries the plaintext token, shown exactly once.
struct APITokenCreated: Codable, Sendable {
    let id: UUID
    let label: String?
    let token: String
}

/// The one place server timestamps become Dates. Parsing is ISO-8601 via
/// Foundation (fractional seconds, "Z" and "+00:00", plus the bare `YYYY-MM-DD`
/// the freezer stores); anything the parser can't read comes back nil, so a
/// format change degrades a caption, never a screen. Labels are date
/// formatters with localized templates, which is how German gets its dotted
/// "2. Aug. 2026" without anyone hand-rolling punctuation.
enum TimestampLabel {
    /// Parses an ISO-8601 server timestamp into a Date. Strings without a
    /// zone are read as UTC — the frame the server itself writes, which is
    /// what the labels below render, so a caption says the same date wherever
    /// in the world the app is opened.
    static func date(_ timestamp: String?) -> Date? {
        guard let timestamp else { return nil }

        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: timestamp) { return date }

        let plain = ISO8601DateFormatter()
        plain.formatOptions = [.withInternetDateTime]
        if let date = plain.date(from: timestamp) { return date }

        // Zone-less forms, in the server's own frame: a full timestamp, then
        // the one bare date `frozenOn` uses. Non-lenient by default, so a
        // nonsense month is rejected rather than silently normalised.
        let utc = TimeZone(identifier: "UTC")!
        for format in ["yyyy-MM-dd'T'HH:mm:ss", "yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd"] {
            let formatter = DateFormatter()
            formatter.locale = Locale(identifier: "en_US_POSIX")
            formatter.timeZone = utc
            formatter.dateFormat = format
            if let date = formatter.date(from: timestamp) { return date }
        }
        return nil
    }

    /// "2 Aug 2026" — abbreviated month, for lists of records (invites,
    /// tokens, previous shops).
    static func day(_ timestamp: String?, locale: Locale = .current) -> String? {
        label(timestamp, template: "yMMMd", locale: locale)
    }

    /// "2 August 2026" — spelled out, for the one place a date is worth its
    /// length (the invite expiry).
    static func long(_ timestamp: String?, locale: Locale = .current) -> String? {
        label(timestamp, template: "yMMMMd", locale: locale)
    }

    /// "Aug 2026" — the "last cooked" caption.
    static func month(_ timestamp: String?, locale: Locale = .current) -> String? {
        label(timestamp, template: "yMMM", locale: locale)
    }

    private static func label(_ timestamp: String?, template: String, locale: Locale) -> String? {
        guard let date = date(timestamp) else { return nil }
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.setLocalizedDateFormatFromTemplate(template)
        return formatter.string(from: date)
    }
}

struct AuthResponse: Codable, Sendable {
    let token: String
    let user: UserProfile
}

/// Result of DELETE /auth/me. `householdDeleted` is true when the account was
/// the last member and the household's shared data went with it.
struct AccountDeleted: Codable, Sendable {
    let householdDeleted: Bool
    let detail: String
}

/// What the server expects of native clients (GET /client-config). Builds
/// below `minIosBuild` are refused with 426 on everything except the
/// offline-queue endpoints — that hard block is the only upgrade surface left;
/// the soft banner that used `currentIosBuild` was removed as noise and
/// nothing reads it any more.
struct ClientConfig: Codable, Equatable, Sendable {
    let apiVersion: String
    let minIosBuild: Int
    let currentIosBuild: Int
    let upgradeUrl: String?
    /// Whether this server can send email at all. Optional is load-bearing: a
    /// server older than this build doesn't send the key, and absent has to
    /// mean "assume it works" rather than a decode failure that would take the
    /// whole config — including the upgrade floor — down with it.
    let passwordResetEnabled: Bool?
    /// Whether the server serves the built-in AI assistant. Optional for the
    /// same reason, but absent means *off* — unlike password resets, a server
    /// that predates the assistant really doesn't have one, and the chat tab
    /// must show its disabled state rather than 404-collect.
    let assistantEnabled: Bool?
}

struct RecipeSummary: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let title: String
    let sourceUrl: String?
    let servings: Int?
    let prepMinutes: Int?
    let cookMinutes: Int?
    let tags: [String]
    /// Optional here as well as on `Recipe`: a backend that predates thumbnails
    /// on the listing simply doesn't send it.
    var imageUrl: String? = nil
    // Optional so the app still decodes against a backend that predates the
    // cooked-history fields.
    var timesCooked: Int? = nil
    var lastCookedAt: String? = nil
    /// Only present when the recipe is read as part of a meal: the batch-cooking
    /// multiple for *that* meal (#32). nil means ×1, which is what a backend
    /// without scaling means too.
    var scale: Double? = nil
    /// How many that scaled share feeds (#53) — `servings` above stays the
    /// recipe's own figure, so this is a separate key rather than a redefined
    /// one. nil on a backend without it, or a recipe that doesn't say.
    var scaledServings: Int? = nil

    var totalMinutes: Int? {
        let total = (prepMinutes ?? 0) + (cookMinutes ?? 0)
        return total > 0 ? total : nil
    }

    var cookedSummary: String? { CookedHistory.summary(times: timesCooked, lastCookedAt: lastCookedAt) }
}

/// "cooked 12×" / "last cooked in May" — the phrasing for a recipe's or meal's
/// usage count (issue #13). Never says "never cooked": a library full of
/// zeroes shouldn't nag.
enum CookedHistory {
    static func summary(times: Int?, lastCookedAt: String?) -> String? {
        guard let times, times > 0 else { return nil }
        let count = String(localized: "cooked \(times)\u{00D7}")
        guard let month = monthLabel(lastCookedAt) else { return count }
        return String(localized: "\(count) \u{00B7} last \(month)")
    }

    /// Timestamps decode as strings app-wide (see the note above), so the
    /// month is read out of one by `TimestampLabel`'s formatter.
    static func monthLabel(_ timestamp: String?) -> String? {
        TimestampLabel.month(timestamp)
    }
}

/// Is the posh version of an ingredient worth the money? Recorded once per
/// ingredient and surfaced where the decision is actually made — standing in
/// front of the shelf.
enum ValueTier: String, CaseIterable, Identifiable, Sendable {
    case premium
    case budget
    case any

    var id: String { rawValue }

    /// Tolerant of missing/unknown values so older caches and backends render.
    init(raw: String?) {
        self = ValueTier(rawValue: raw ?? "") ?? .any
    }

    var label: String {
        switch self {
        case .premium: String(localized: "Worth paying up for")
        case .budget: String(localized: "Own-brand is fine")
        case .any: String(localized: "No strong opinion")
        }
    }

    var short: String {
        switch self {
        case .premium: String(localized: "Premium")
        case .budget: String(localized: "Budget")
        case .any: String(localized: "No opinion")
        }
    }

    var badge: String {
        switch self {
        case .premium: "⭐"
        case .budget: "💷"
        case .any: ""
        }
    }
}

struct RecipeLine: Codable, Identifiable, Equatable, Sendable {
    let ingredientId: UUID
    let name: String
    let aisle: String
    let isStaple: Bool
    let quantity: Double?
    let unit: String?
    let display: String
    let raw: String?
    // Optional so responses from an older backend still decode.
    var valueTier: String? = nil
    var valueNote: String? = nil

    var id: UUID { ingredientId }
    var tier: ValueTier { ValueTier(raw: valueTier) }
}

struct Recipe: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let title: String
    let sourceUrl: String?
    let servings: Int?
    let prepMinutes: Int?
    let cookMinutes: Int?
    let imageUrl: String?
    let instructions: String?
    let tags: [String]
    let parseSource: String
    let edited: Bool
    let ingredients: [RecipeLine]
    var timesCooked: Int? = nil
    var lastCookedAt: String? = nil

    var cookedSummary: String? { CookedHistory.summary(times: timesCooked, lastCookedAt: lastCookedAt) }
}

struct IngestResponse: Codable, Sendable {
    let recipe: Recipe
    let cached: Bool
}

struct Meal: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let name: String
    let slot: String?
    let recipes: [RecipeSummary]
    let looseIngredients: [RecipeLine]
    var timesCooked: Int? = nil
    var lastCookedAt: String? = nil

    var cookedSummary: String? { CookedHistory.summary(times: timesCooked, lastCookedAt: lastCookedAt) }
}

struct PlanMeal: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let meal: Meal
    let cookedAt: String?
}

struct Plan: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let label: String
    let status: String
    let meals: [PlanMeal]
    // Optional so caches written by older app versions still decode.
    var archivedAt: String? = nil

    var slots: [(slot: String, meals: [PlanMeal])] {
        let grouped = Dictionary(grouping: meals) { $0.meal.slot ?? "other" }
        return grouped.keys.sorted().map { (slot: $0, meals: grouped[$0] ?? []) }
    }
}

struct PlanSummary: Codable, Identifiable, Equatable, Hashable, Sendable {
    let id: UUID
    let label: String
    let startsOn: String?
    let status: String
    let mealCount: Int
}

struct ItemSource: Codable, Equatable, Sendable {
    let adHoc: Bool
    let mealName: String?
    let recipeTitle: String?
    let quantity: Double?
    // Optional so caches written by older app versions still decode.
    var mealId: UUID? = nil
    var recipeId: UUID? = nil
}

struct ListItem: Codable, Identifiable, Equatable, Sendable {
    var id: UUID
    var ingredientId: UUID
    var name: String
    var aisle: String
    var aisleLabel: String
    var isStaple: Bool
    var quantity: Double?
    var unit: String?
    var display: String
    var checked: Bool
    var excluded: Bool
    // Staple marked "I'm low" in the staples check — shown on the main list.
    // Optional so caches written by older app versions still decode.
    var stapleNeeded: Bool? = nil
    var valueTier: String? = nil
    var valueNote: String? = nil
    var sources: [ItemSource]

    var neededBy: [String] {
        Array(Set(sources.compactMap(\.mealName))).sorted()
    }

    var isNeededStaple: Bool { stapleNeeded ?? false }
    var tier: ValueTier { ValueTier(raw: valueTier) }

    /// Deletable: nothing but hand-adds put it here (matches the server's own
    /// `is_adhoc_only` — a meal-sourced line would 409).
    var isAdhocOnly: Bool { sources.allSatisfy(\.adHoc) }
}

struct ShoppingListPayload: Codable, Equatable, Sendable {
    let id: UUID
    let status: String
    var items: [ListItem]
    let hiddenStaples: Int
}

struct Aisle: Codable, Equatable, Hashable, Sendable {
    let emoji: String
    let label: String
}

/// A saved store and its aisle walking order (`GET /supermarkets`). The
/// active one's order is what the shopping list sorts by and what `/aisles`
/// returns — the rest of the app follows along without knowing supermarkets
/// exist.
struct Supermarket: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let name: String
    let aisleOrder: [String]
    let isActive: Bool
}

/// One batch in the freezer (`GET /freezer`, decision Q24): what it is, how
/// many portions are left, and when it went in. `mealId` / `recipeId` say
/// where it came from while that meal or recipe still exists; both nil is free
/// text — food that never went through the plan.
struct FreezerItem: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let label: String
    let mealId: UUID?
    let recipeId: UUID?
    let portions: Int
    let note: String?
    /// A bare date, `YYYY-MM-DD`.
    let frozenOn: String

    var portionsText: String {
        portions == 1
            ? String(localized: "1 portion")
            : String(localized: "\(portions) portions")
    }

    var frozenOnDate: Date? {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .iso8601)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = .current
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.date(from: frozenOn)
    }

    /// "frozen 3 weeks ago" — the age is the point of the row. The relative
    /// formatter speaks the device language; only the "frozen" frame is ours.
    var frozenText: String {
        guard let date = frozenOnDate else { return String(localized: "frozen \(frozenOn)") }
        if Calendar.current.isDateInToday(date) { return String(localized: "frozen today") }
        let formatter = RelativeDateTimeFormatter()
        formatter.unitsStyle = .full
        return String(localized: "frozen \(formatter.localizedString(for: date, relativeTo: .now))")
    }
}

/// The whole freezer, oldest batch first — that is the one to eat next.
struct FreezerPayload: Codable, Equatable, Sendable {
    let items: [FreezerItem]
    let totalPortions: Int
}

/// A finished shop (`GET /shopping-list/archived`) — what a list looked like
/// when it was archived, for the record.
struct ArchivedListSummary: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let createdAt: String
    let archivedAt: String?
    let itemCount: Int
}

/// Ingredient-level metadata (canonical name, aisle, staple flag, premium-vs-
/// budget advice) — shared by every recipe line and list item that references
/// the ingredient.
struct IngredientInfo: Codable, Identifiable, Equatable, Sendable {
    let id: UUID
    let name: String
    var aisle: String
    var aisleLabel: String
    var isStaple: Bool
    // Optional so responses from an older backend still decode.
    var valueTier: String? = nil
    var valueNote: String? = nil

    var tier: ValueTier { ValueTier(raw: valueTier) }
}

/// Ingredients that are the same food under two spellings
/// (`GET /ingredients/duplicates`). The keeper is the suggested survivor;
/// groups are name-folding facts, not guesses — "beef mince" vs "minced
/// beef" never appears here and needs a manual merge.
struct DuplicateGroup: Codable, Equatable, Sendable {
    let canonicalName: String
    let keeper: IngredientInfo
    let duplicates: [IngredientInfo]
}

/// An ingredient stored under a name a new write wouldn't use, with no twin
/// to merge into — tidied by creating the canonical row and folding this one
/// into it.
struct UnfoldedIngredient: Codable, Equatable, Sendable {
    let ingredient: IngredientInfo
    let canonicalName: String
}

struct DuplicatesPayload: Codable, Equatable, Sendable {
    let groups: [DuplicateGroup]
    let unfolded: [UnfoldedIngredient]
}

/// `POST /ingredients/{id}/merge` — the survivor and how many rows it absorbed.
struct MergeResult: Codable, Equatable, Sendable {
    let ingredient: IngredientInfo
    let merged: Int
}

/// A loose ingredient being written (meal sides, decision F1/F2): name plus
/// an optional quantity in the API's convention units.
struct LooseLine: Identifiable, Equatable, Sendable {
    let id = UUID()
    var name: String
    var quantity: Double?
    var unit: String?

    var display: String {
        let amount = ShoppingListStore.displayQuantity(quantity, unit)
        return amount.isEmpty ? name : "\(name) — \(amount)"
    }
}

// Fallback store-walking order used until /aisles has been fetched once.
enum AisleOrder {
    static let fallback = ["🥬", "🍞", "🥩", "❄️", "🥛", "🥫", "🍝", "🌶️", "🥤", "🍫", "🧊", "🧼", "🧴", "❓"]
}
