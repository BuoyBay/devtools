// Capture native pasteboard bytes after a deliberate IDE Copy action.
import AppKit
import Foundation
guard CommandLine.arguments.count == 2 || (CommandLine.arguments.count == 3 && CommandLine.arguments[2] == "--allow-any-type") else {
    fputs("Usage: capture.swift directory [--allow-any-type]\n", stderr)
    exit(1)
}
let allowAnyType = CommandLine.arguments.count == 3
let directory = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
let board = NSPasteboard.general
let known = ["com.realsoftware.realbasic.class", "com.xojo.code.legacybinaryclass", "CorePasteboardFlavorType 0x4362436C", "CorePasteboardFlavorType 0x52534349", "CorePasteboardFlavorType 0x43625677", "CorePasteboardFlavorType 0x43624D74", "CorePasteboardFlavorType 0x52424354"]
guard allowAnyType || (board.types ?? []).contains(where: { known.contains($0.rawValue) }) else {
    fputs("No RB IDE binary selection is currently on the clipboard.\n", stderr)
    exit(1)
}
let generation = board.changeCount
let offered = board.types ?? []
guard !offered.isEmpty else { fputs("Clipboard is empty.\n", stderr); exit(1) }
let inventory = try JSONSerialization.data(withJSONObject: offered.map { $0.rawValue }, options: [.prettyPrinted])
try inventory.write(to: directory.appendingPathComponent("offered-types.json"))
var entries: [[String: Any]] = []
for (index, type) in offered.enumerated() {
    guard let data = board.data(forType: type) else {
        fputs("Could not read offered clipboard type: \(type.rawValue)\n", stderr)
        exit(1)
    }
    let file = "format-\(index).bin"
    try data.write(to: directory.appendingPathComponent(file))
    entries.append(["type": type.rawValue, "file": file, "length": data.count])
}
guard board.changeCount == generation else {
    fputs("Clipboard changed during capture.\n", stderr)
    exit(1)
}
let manifest = try JSONSerialization.data(withJSONObject: entries, options: [.prettyPrinted, .sortedKeys])
try manifest.write(to: directory.appendingPathComponent("manifest.json"))
print(String(data: manifest, encoding: .utf8)!)
