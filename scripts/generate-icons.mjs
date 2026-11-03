// Deterministic, dependency-free macOS icon with transparent outer padding.
import fs from 'node:fs'
import path from 'node:path'
import zlib from 'node:zlib'

const root = path.resolve(import.meta.dirname, '..')
const size = 512
const samples = 4
const scale = 1024 / size
const pixels = Buffer.alloc(size * size * 4)
const nodes = [
  [266, 512, 69, [39, 143, 109]],
  [512, 266, 86, [39, 143, 109]],
  [758, 512, 69, [61, 115, 162]],
  [512, 758, 86, [204, 151, 58]],
  [512, 512, 56, [39, 143, 109]],
]
const edges = [[0, 1], [1, 2], [2, 3], [3, 0], [0, 4], [2, 4]].map(([a, b]) => {
  const [ax, ay] = nodes[a], [bx, by] = nodes[b]
  return { ax, ay, dx: bx - ax, dy: by - ay, length: (bx - ax) ** 2 + (by - ay) ** 2 }
})
const clamp = value => Math.max(0, Math.min(1, value))
const tileDistance = (x, y) => {
  const qx = Math.abs(x - 512) - 256
  const qy = Math.abs(y - 512) - 256
  return Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) + Math.min(Math.max(qx, qy), 0) - 200
}

for (let py = 0; py < size; py++) for (let px = 0; px < size; px++) {
  const sum = [0, 0, 0, 0]
  for (let sy = 0; sy < samples; sy++) for (let sx = 0; sx < samples; sx++) {
    const x = (px + (sx + 0.5) / samples) * scale
    const y = (py + (sy + 0.5) / samples) * scale
    const shadowDistance = Math.max(0, tileDistance(x, y - 12))
    let alpha = 0.15 * Math.exp(-(shadowDistance ** 2) / (2 * 13 ** 2))
    let red = 39 * alpha, green = 49 * alpha, blue = 57 * alpha
    const blend = (r, g, b, opacity) => {
      red = r * opacity + red * (1 - opacity)
      green = g * opacity + green * (1 - opacity)
      blue = b * opacity + blue * (1 - opacity)
      alpha = opacity + alpha * (1 - opacity)
    }
    const distance = tileDistance(x, y)
    const cover = clamp(0.5 - distance)
    if (cover > 0) {
      const t = clamp((y - 56) / 912)
      const rim = clamp(distance + 1.5) * 5
      blend(255 - 9 * t - rim, 255 - 8 * t - rim, 255 - 6 * t - rim, cover)
      for (const edge of edges) {
        const t = clamp(((x - edge.ax) * edge.dx + (y - edge.ay) * edge.dy) / edge.length)
        const line = clamp(15.5 - Math.hypot(x - edge.ax - t * edge.dx, y - edge.ay - t * edge.dy))
        if (line > 0) blend(151, 184, 174, line * cover)
      }
      for (const [cx, cy, radius, color] of nodes) {
        const circle = clamp(radius + 0.5 - Math.hypot(x - cx, y - cy))
        if (circle > 0) {
          const light = 5 * clamp((cy + radius - y) / (2 * radius))
          blend(color[0] + light, color[1] + light, color[2] + light, circle * cover)
        }
      }
    }
    sum[0] += red; sum[1] += green; sum[2] += blue; sum[3] += alpha
  }
  const i = (py * size + px) * 4
  if (sum[3] > 0) for (let c = 0; c < 3; c++) pixels[i + c] = Math.round(sum[c] / sum[3])
  pixels[i + 3] = Math.round(sum[3] / (samples ** 2) * 255)
}

const crc = bytes => {
  let value = 0xffffffff
  for (const byte of bytes) {
    value ^= byte
    for (let bit = 0; bit < 8; bit++) value = (value >>> 1) ^ ((value & 1) ? 0xedb88320 : 0)
  }
  return (value ^ 0xffffffff) >>> 0
}
const chunk = (type, data) => {
  const tag = Buffer.from(type), buffer = Buffer.alloc(data.length + 12)
  buffer.writeUInt32BE(data.length); tag.copy(buffer, 4); data.copy(buffer, 8)
  buffer.writeUInt32BE(crc(Buffer.concat([tag, data])), data.length + 8)
  return buffer
}
const rows = Buffer.alloc((size * 4 + 1) * size)
for (let y = 0; y < size; y++) pixels.copy(rows, y * (size * 4 + 1) + 1, y * size * 4, (y + 1) * size * 4)
const header = Buffer.alloc(13)
header.writeUInt32BE(size); header.writeUInt32BE(size, 4); header[8] = 8; header[9] = 6
const png = Buffer.concat([
  Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]), chunk('IHDR', header),
  chunk('IDAT', zlib.deflateSync(rows)), chunk('IEND', Buffer.alloc(0)),
])
for (const file of ['src-tauri/icons/icon.png', 'public/icon.png']) {
  const target = path.join(root, file)
  fs.mkdirSync(path.dirname(target), { recursive: true })
  fs.writeFileSync(target, png)
}
console.log('Generated rounded SessionGraph icons (512 × 512 RGBA)')
