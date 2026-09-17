import SwiftUI

/// A hand-rolled markdown renderer for recipe instructions.
///
/// The app carries no third-party packages (see CREDITS.md), so this is
/// deliberately a subset: headings, bullet and numbered lists, fenced code
/// blocks, and the inline styles `AttributedString(markdown:)` already
/// understands — **bold**, *italics*, `code`, links. Anything else renders as
/// the plain text it is, so a recipe never loses content, only decoration.
/// The parsed form lives in `MarkdownBlock`/`parseMarkdownBlocks` so the
/// rules are unit-testable without a view hierarchy.
struct MarkdownText: View {
    let markdown: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(parseMarkdownBlocks(markdown).enumerated()), id: \.offset) { _, block in
                blockView(block)
            }
        }
    }

    @ViewBuilder
    private func blockView(_ block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(inline(text))
                .font(level == 1 ? .title3.bold() : level == 2 ? .headline : .subheadline.bold())
        case .paragraph(let text):
            Text(inline(text))
        case .bullet(let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(verbatim: "•")
                Text(inline(text))
            }
        case .ordered(let marker, let text):
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(verbatim: marker)
                Text(inline(text))
            }
        case .code(let lines):
            Text(lines.joined(separator: "\n"))
                .font(.callout.monospaced())
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    /// Inline markdown only — block structure is ours, so the parser must not
    /// swallow list markers or headings the way full-document parsing would.
    private func inline(_ text: String) -> AttributedString {
        guard let parsed = try? AttributedString(
            markdown: text,
            options: AttributedString.MarkdownParsingOptions(
                interpretedSyntax: .inlineOnlyPreservingWhitespace
            )
        ) else { return AttributedString(text) }
        return parsed
    }
}

/// One rendered chunk of a markdown document. Paragraphs are kept as whole
/// paragraphs (soft-wrapped by SwiftUI); list items are one block each so a
/// recipe's numbered steps stay separate rows.
enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bullet(String)
    case ordered(marker: String, text: String)
    case code([String])
}

/// Markdown subset → blocks. Line-based on purpose: recipe instructions are
/// written in an editor one line at a time, and a line that isn't recognised
/// simply becomes (part of) a paragraph.
func parseMarkdownBlocks(_ text: String) -> [MarkdownBlock] {
    var blocks: [MarkdownBlock] = []
    var paragraph: [String] = []
    var fence: [String] = []
    var inFence = false

    func flushParagraph() {
        guard !paragraph.isEmpty else { return }
        blocks.append(.paragraph(paragraph.joined(separator: " ")))
        paragraph = []
    }
    func flushFence() {
        guard !fence.isEmpty else { return }
        blocks.append(.code(fence))
        fence = []
    }

    for rawLine in text.components(separatedBy: "\n") {
        let line = rawLine.trimmingCharacters(in: .whitespaces)
        if line.hasPrefix("```") {
            flushParagraph()
            if inFence { flushFence() }
            inFence.toggle()
            continue
        }
        if inFence {
            fence.append(line)
            continue
        }
        if line.isEmpty {
            flushParagraph()
            continue
        }
        if let (level, rest) = MarkdownBlock.heading(line) {
            flushParagraph()
            blocks.append(.heading(level: level, text: rest))
            continue
        }
        if let rest = MarkdownBlock.bullet(line) {
            flushParagraph()
            blocks.append(.bullet(rest))
            continue
        }
        if let (marker, rest) = MarkdownBlock.ordered(line) {
            flushParagraph()
            blocks.append(.ordered(marker: marker, text: rest))
            continue
        }
        paragraph.append(line)
    }
    flushParagraph()
    flushFence()
    return blocks
}

extension MarkdownBlock {
    /// "#", "##", "### " — capped at 3 visual levels; deeper "#"-runs degrade
    /// to level 3 rather than disappearing.
    static func heading(_ line: String) -> (level: Int, text: String)? {
        let hashes = line.prefix { $0 == "#" }
        guard (1...6).contains(hashes.count), line.dropFirst(hashes.count).hasPrefix(" ") else { return nil }
        let body = line.dropFirst(hashes.count).trimmingCharacters(in: .whitespaces)
        guard !body.isEmpty else { return nil }
        return (min(hashes.count, 3), String(body))
    }

    /// "- item" / "* item" / "+ item"
    static func bullet(_ line: String) -> String? {
        guard line.count >= 2,
              let first = line.first, first == "-" || first == "*" || first == "+",
              line[line.index(after: line.startIndex)] == " "
        else { return nil }
        let body = line.dropFirst(2).trimmingCharacters(in: .whitespaces)
        return body.isEmpty ? nil : body
    }

    /// "1. step" / "1) step" — the marker the author typed is kept, so a
    /// deliberately numbered recipe stays numbered as written.
    static func ordered(_ line: String) -> (marker: String, text: String)? {
        let digits = line.prefix { $0.isNumber }
        guard (1...3).contains(digits.count), line.count > digits.count else { return nil }
        let separator = line[line.index(line.startIndex, offsetBy: digits.count)]
        guard separator == "." || separator == ")", line.count > digits.count + 1,
              line[line.index(after: line.index(line.startIndex, offsetBy: digits.count))] == " "
        else { return nil }
        let body = line.dropFirst(digits.count + 2).trimmingCharacters(in: .whitespaces)
        guard !body.isEmpty else { return nil }
        return ("\(digits)\(separator)", body)
    }
}
