import Foundation

/// The quantity/unit convention (decision Q2) as the app needs to know it:
/// what to offer in a picker, and how to read an amount out of free text.
///
/// The vocabulary is a *convenience*, never a gate — `unit` stays a plain
/// String everywhere so a unit the server knows and this build doesn't can
/// still be typed, sent and displayed.
enum MealsUnits {
    /// Metric first (canonical g/ml, plus the kg/l the API converts for us),
    /// then the natural counts people actually shop in.
    static let common = ["g", "kg", "ml", "l", "item", "tin", "pack", "clove", "bunch", "slice", "jar", "bottle"]

    /// Units the API rejects (decision Q2), with the conversion it would
    /// quote back. Checked as the user types so the correction arrives at
    /// the field rather than after the whole meal or recipe fails to save —
    /// the app never converts anything, it only refuses what the server
    /// would refuse. The conversion is a real Measurement, and Foundation
    /// renders it in the device's language ("1 tsp = 5 ml" at home, "1 TL
    /// = 5 ml" in German); the one entry that isn't a quantity is a catalog
    /// sentence.
    static let rejected: [String: UnitRejection] = [
        "tsp": .volume(1, .teaspoons, 5, .milliliters),
        "teaspoon": .volume(1, .teaspoons, 5, .milliliters),
        "tbsp": .volume(1, .tablespoons, 15, .milliliters),
        "tablespoon": .volume(1, .tablespoons, 15, .milliliters),
        "cup": .volume(1, .cups, 240, .milliliters),
        "cups": .volume(1, .cups, 240, .milliliters),
        "oz": .mass(1, .ounces, 28, .grams),
        "ounce": .mass(1, .ounces, 28, .grams),
        "lb": .mass(1, .pounds, 454, .grams),
        "pound": .mass(1, .pounds, 454, .grams),
        "pint": .volume(1, .imperialPints, 568, .milliliters),
        "stick": .note("sticks aren't metric — use g or ml"),
    ]

    /// nil when the unit is fine; otherwise the conversion to show.
    static func rejection(for unit: String?, locale: Locale = .current) -> String? {
        guard let unit, !unit.isEmpty else { return nil }
        guard let rejection = rejected[unit.lowercased().trimmingCharacters(in: .whitespaces)] else { return nil }
        return rejection.text(locale: locale)
    }
}

/// Why a unit can't be sent, and how to say so.
enum UnitRejection: Equatable {
    /// A volume-to-volume conversion like "1 tsp = 5 ml".
    case volume(Double, UnitVolume, Double, UnitVolume)
    /// A mass-to-mass conversion like "1 oz = 28 g".
    case mass(Double, UnitMass, Double, UnitMass)
    /// The one entry that isn't a quantity, kept as a catalog sentence.
    case note(String)

    /// The sentence, with both measurements rendered by Foundation's
    /// measurement formatting — the units' words are the locale's, not ours
    /// to translate. Spelled out, because the abbreviations some locales
    /// pick ("c" for a cup) are cryptic in a correction hint.
    func text(locale: Locale) -> String {
        switch self {
        case let .volume(fromValue, fromUnit, toValue, toUnit):
            return Self.rendered(fromValue, fromUnit, toValue, toUnit, locale: locale)
        case let .mass(fromValue, fromUnit, toValue, toUnit):
            return Self.rendered(fromValue, fromUnit, toValue, toUnit, locale: locale)
        case let .note(key):
            return String(localized: String.LocalizationValue(key))
        }
    }

    private static func rendered<UnitType: Dimension>(
        _ fromValue: Double, _ fromUnit: UnitType, _ toValue: Double, _ toUnit: UnitType, locale: Locale
    ) -> String {
        let style = Measurement<UnitType>.FormatStyle(width: .wide, usage: .asProvided).locale(locale)
        let from = Measurement(value: fromValue, unit: fromUnit).formatted(style)
        let to = Measurement(value: toValue, unit: toUnit).formatted(style)
        return "\(from) = \(to)"
    }
}

/// Reads "200 g frozen peas" — and, since #30, "frozen peas 200g" — into its
/// parts. Used by the shopping list's quick add and by the meal editor's side
/// entry, which is why it lives here rather than in either view.
///
/// The glued form is the one people type off a packet, and the old parser
/// silently folded it into the *name*, minting a canonical ingredient called
/// "frozen peas 200g". Anything genuinely ambiguous still comes back as a bare
/// name — the explicit amount fields are the answer there, not more guessing.
enum QuantityParser {
    static func parse(_ text: String) -> (name: String, quantity: Double?, unit: String?) {
        let words = text.split(separator: " ").map(String.init)
        guard words.count >= 2 else { return (text, nil, nil) }

        // "frozen peas 200 g"
        if words.count >= 3, let quantity = Double(words[words.count - 2]) {
            return (words.dropLast(2).joined(separator: " "), quantity, words[words.count - 1])
        }
        // "200 g frozen peas"
        if words.count >= 3, let quantity = Double(words[0]) {
            return (words.dropFirst(2).joined(separator: " "), quantity, words[1])
        }
        // "frozen peas 200g"
        if let glued = splitGlued(words[words.count - 1]), words.count >= 2 {
            return (words.dropLast().joined(separator: " "), glued.0, glued.1)
        }
        // "200g frozen peas"
        if let glued = splitGlued(words[0]) {
            return (words.dropFirst().joined(separator: " "), glued.0, glued.1)
        }
        // "6 eggs" — a count of a thing, which is a natural unit (Q2). Without
        // this the whole string becomes an ingredient named "6 eggs".
        if words.count == 2, let quantity = Double(words[0]) {
            return (words[1], quantity, "item")
        }
        return (text, nil, nil)
    }

    /// "200g" → (200, "g"). nil for anything that isn't digits-then-letters.
    private static func splitGlued(_ word: String) -> (Double, String)? {
        let digits = word.prefix { $0.isNumber || $0 == "." }
        let rest = word.dropFirst(digits.count)
        guard !digits.isEmpty, !rest.isEmpty, rest.allSatisfy({ $0.isLetter }),
              let quantity = Double(digits)
        else { return nil }
        return (quantity, String(rest))
    }
}
