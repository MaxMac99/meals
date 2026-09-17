import XCTest
@testable import Meals

/// The markdown subset recipe instructions render with. The contract that
/// matters: anything not recognised still reaches the reader as text — a
/// recipe can lose decoration, never content.
final class MarkdownTests: XCTestCase {
    func testHeadingsLoseTheirHashes() {
        XCTAssertEqual(
            parseMarkdownBlocks("# First\n## Second\n### Third"),
            [
                .heading(level: 1, text: "First"),
                .heading(level: 2, text: "Second"),
                .heading(level: 3, text: "Third"),
            ]
        )
    }

    func testDeepHeadingsDegradeInsteadOfVanishing() {
        XCTAssertEqual(
            parseMarkdownBlocks("#### four"),
            [.heading(level: 3, text: "four")]
        )
    }

    func testBulletsAndOrderedItemsBecomeTheirOwnBlocks() {
        XCTAssertEqual(
            parseMarkdownBlocks("- boil\n* simmer\n+ rest\n1. serve\n2) repeat"),
            [
                .bullet("boil"),
                .bullet("simmer"),
                .bullet("rest"),
                .ordered(marker: "1.", text: "serve"),
                .ordered(marker: "2)", text: "repeat"),
            ]
        )
    }

    func testConsecutiveLinesJoinIntoOneParagraph() {
        XCTAssertEqual(
            parseMarkdownBlocks("brown the\nmince first"),
            [.paragraph("brown the mince first")]
        )
    }

    func testBlankLinesSeparateParagraphs() {
        XCTAssertEqual(
            parseMarkdownBlocks("step one\n\nstep two"),
            [.paragraph("step one"), .paragraph("step two")]
        )
    }

    func testFencedCodeKeepsItsLines() {
        XCTAssertEqual(
            parseMarkdownBlocks("```\n200 g flour\nrest\n```"),
            [.code(["200 g flour", "rest"])]
        )
    }

    func testUnterminatedFenceStillRenders() {
        XCTAssertEqual(parseMarkdownBlocks("```\n200 g flour"), [.code(["200 g flour"])])
    }

    func testUnrecognisedMarkersStayVisibleText() {
        // A stray ">" or a hyphen with no space is prose, not structure.
        XCTAssertEqual(
            parseMarkdownBlocks("> quoted?\n-half measures"),
            [.paragraph("> quoted? -half measures")]
        )
    }

    func testEmptyInputYieldsNothing() {
        XCTAssertEqual(parseMarkdownBlocks(""), [])
        XCTAssertEqual(parseMarkdownBlocks("   \n  \n"), [])
    }

    func testMarkdownSymbolsAreNotStructuralMidParagraph() {
        // "a - b" is a sentence with a dash, not a list.
        XCTAssertEqual(parseMarkdownBlocks("a - b"), [.paragraph("a - b")])
    }
}
