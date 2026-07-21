// Generates the 1024px source image used to build AppIcon.icns. Keeping this vector-like
// drawing in source avoids checking generated binary artwork into the repository.
//
// The artwork is a 3/4 view of the GL.iNet Comet Pro (GL-RM10): a compact charcoal cube with an
// angled front touchscreen, a status LED, and side vents — the hardware this app pairs with.

import AppKit

guard CommandLine.arguments.count == 2 else {
    fputs("usage: make-icon.swift <output.png>\n", stderr)
    exit(2)
}

func polygon(_ points: [NSPoint]) -> NSBezierPath {
    let path = NSBezierPath()
    path.move(to: points[0])
    for point in points.dropFirst() { path.line(to: point) }
    path.close()
    return path
}

let size = NSSize(width: 1024, height: 1024)
let image = NSImage(size: size)
image.lockFocus()
NSGraphicsContext.current?.imageInterpolation = .high

// Rounded-square app-icon tile with a deep blue gradient and a soft glow.
let tile = NSBezierPath(roundedRect: NSRect(x: 54, y: 54, width: 916, height: 916), xRadius: 210, yRadius: 210)
NSGradient(colors: [
    NSColor(red: 0.055, green: 0.10, blue: 0.19, alpha: 1),
    NSColor(red: 0.075, green: 0.28, blue: 0.62, alpha: 1),
])!.draw(in: tile, angle: -42)
tile.setClip()
let glow = NSBezierPath(ovalIn: NSRect(x: 360, y: 470, width: 660, height: 660))
NSColor(red: 0.25, green: 0.62, blue: 1, alpha: 0.14).setFill()
glow.fill()

// Comet Pro cube. Front face is axis-aligned; the body extends up-and-right for the 3/4 view.
let fbl = NSPoint(x: 250, y: 330), fbr = NSPoint(x: 626, y: 330)
let ftl = NSPoint(x: 250, y: 612), ftr = NSPoint(x: 626, y: 612)
let depth = NSPoint(x: 168, y: 104)
let btl = NSPoint(x: ftl.x + depth.x, y: ftl.y + depth.y)
let btr = NSPoint(x: ftr.x + depth.x, y: ftr.y + depth.y)
let bbr = NSPoint(x: fbr.x + depth.x, y: fbr.y + depth.y)

NSGraphicsContext.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.42)
shadow.shadowBlurRadius = 46
shadow.shadowOffset = NSSize(width: 0, height: -24)
shadow.set()

// Side (right) face — darkest.
let side = polygon([ftr, btr, bbr, fbr])
NSColor(red: 0.085, green: 0.10, blue: 0.125, alpha: 1).setFill()
side.fill()
NSGraphicsContext.restoreGraphicsState()

// Top face — lightest, catching the light.
let top = polygon([ftl, ftr, btr, btl])
NSGradient(colors: [
    NSColor(red: 0.26, green: 0.29, blue: 0.35, alpha: 1),
    NSColor(red: 0.17, green: 0.20, blue: 0.25, alpha: 1),
])!.draw(in: top, angle: -70)

// Front face — mid charcoal.
let front = polygon([fbl, fbr, ftr, ftl])
NSGradient(colors: [
    NSColor(red: 0.17, green: 0.19, blue: 0.23, alpha: 1),
    NSColor(red: 0.11, green: 0.12, blue: 0.15, alpha: 1),
])!.draw(in: front, angle: -90)

// Crisp top edges.
NSColor.white.withAlphaComponent(0.14).setStroke()
top.lineWidth = 4
top.stroke()

// Side vents.
NSColor.black.withAlphaComponent(0.55).setStroke()
for index in 0..<4 {
    let y = CGFloat(470 - index * 40)
    let vent = NSBezierPath()
    vent.move(to: NSPoint(x: 690, y: y))
    vent.line(to: NSPoint(x: 748, y: y + 36))
    vent.lineWidth = 7
    vent.lineCapStyle = .round
    vent.stroke()
}

// Brand hint on the top face.
let brand = NSBezierPath()
brand.move(to: NSPoint(x: 388, y: 664))
brand.line(to: NSPoint(x: 470, y: 664))
brand.lineWidth = 8
brand.lineCapStyle = .round
NSColor.white.withAlphaComponent(0.22).setStroke()
brand.stroke()

// Screen bezel + glowing usage screen on the front face.
let bezel = NSBezierPath(roundedRect: NSRect(x: 292, y: 372, width: 292, height: 190), xRadius: 26, yRadius: 26)
NSColor.black.withAlphaComponent(0.94).setFill()
bezel.fill()
NSColor.white.withAlphaComponent(0.20).setStroke()
bezel.lineWidth = 5
bezel.stroke()

NSGraphicsContext.saveGraphicsState()
let screen = NSBezierPath(roundedRect: NSRect(x: 312, y: 392, width: 252, height: 150), xRadius: 15, yRadius: 15)
screen.setClip()
NSGradient(colors: [
    NSColor(red: 0.96, green: 0.42, blue: 0.28, alpha: 1),
    NSColor(red: 0.24, green: 0.50, blue: 0.98, alpha: 1),
])!.draw(in: NSRect(x: 312, y: 392, width: 252, height: 150), angle: 24)

// A usage bar on the screen: track plus filled portion.
let track = NSBezierPath(roundedRect: NSRect(x: 336, y: 432, width: 204, height: 22), xRadius: 11, yRadius: 11)
NSColor.white.withAlphaComponent(0.30).setFill()
track.fill()
let barFill = NSBezierPath(roundedRect: NSRect(x: 336, y: 432, width: 132, height: 22), xRadius: 11, yRadius: 11)
NSColor.white.withAlphaComponent(0.92).setFill()
barFill.fill()
let secondRow = NSBezierPath(roundedRect: NSRect(x: 336, y: 470, width: 150, height: 16), xRadius: 8, yRadius: 8)
NSColor.white.withAlphaComponent(0.60).setFill()
secondRow.fill()
NSGraphicsContext.restoreGraphicsState()

// Status LED near the front-bottom-right corner.
let led = NSBezierPath(ovalIn: NSRect(x: 588, y: 348, width: 26, height: 26))
NSColor(red: 0.25, green: 0.95, blue: 0.57, alpha: 1).setFill()
led.fill()

// A small four-point sparkle ties the hardware to AI monitoring.
let center = NSPoint(x: 812, y: 760)
let sparkle = NSBezierPath()
sparkle.move(to: NSPoint(x: center.x - 52, y: center.y))
sparkle.curve(to: NSPoint(x: center.x, y: center.y + 52), controlPoint1: NSPoint(x: center.x - 24, y: center.y + 7), controlPoint2: NSPoint(x: center.x - 7, y: center.y + 24))
sparkle.curve(to: NSPoint(x: center.x + 52, y: center.y), controlPoint1: NSPoint(x: center.x + 7, y: center.y + 24), controlPoint2: NSPoint(x: center.x + 24, y: center.y + 7))
sparkle.curve(to: NSPoint(x: center.x, y: center.y - 52), controlPoint1: NSPoint(x: center.x + 24, y: center.y - 7), controlPoint2: NSPoint(x: center.x + 7, y: center.y - 24))
sparkle.curve(to: NSPoint(x: center.x - 52, y: center.y), controlPoint1: NSPoint(x: center.x - 7, y: center.y - 24), controlPoint2: NSPoint(x: center.x - 24, y: center.y - 7))
sparkle.close()
NSColor.white.setFill()
sparkle.fill()

image.unlockFocus()
guard let tiff = image.tiffRepresentation,
      let bitmap = NSBitmapImageRep(data: tiff),
      let png = bitmap.representation(using: .png, properties: [:]) else {
    fputs("could not encode icon\n", stderr)
    exit(1)
}
try png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
