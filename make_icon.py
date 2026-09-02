"""Generates mcscan.ico - an isometric block on a dark rounded tile."""
import os
from PIL import Image, ImageDraw

S = 512
BG = (13, 17, 23, 255)
TOP = (74, 222, 128, 255)
LEFT = (34, 139, 86, 255)
RIGHT = (22, 101, 63, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.22), fill=BG)

cx, cy = S / 2, S / 2 + S * 0.02
w = S * 0.30          # half-width of the block
h = S * 0.17          # half-height of the top face
depth = S * 0.26      # height of the side faces

top = [(cx, cy - h - depth / 2), (cx + w, cy - depth / 2),
       (cx, cy + h - depth / 2), (cx - w, cy - depth / 2)]
left = [(cx - w, cy - depth / 2), (cx, cy + h - depth / 2),
        (cx, cy + h + depth / 2), (cx - w, cy + depth / 2)]
right = [(cx + w, cy - depth / 2), (cx, cy + h - depth / 2),
         (cx, cy + h + depth / 2), (cx + w, cy + depth / 2)]

for face, color in ((left, LEFT), (right, RIGHT), (top, TOP)):
    d.polygon(face, fill=color)

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcscan.ico")
img.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out)
