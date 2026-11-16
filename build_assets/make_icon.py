from PIL import Image, ImageDraw
S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
# gradient background in a macOS-style rounded square
bg = Image.new("RGBA", (S, S))
d = ImageDraw.Draw(bg)
for y in range(S):
    t = y / S
    d.line([(0, y), (S, y)], fill=(int(255 - 40 * t), int(92 - 50 * t), int(92 - 40 * t), 255))
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([100, 100, S - 100, S - 100], radius=185, fill=255)
img.paste(bg, (0, 0), mask)
d = ImageDraw.Draw(img)
W = (255, 255, 255, 255)
# double music note
d.ellipse([290, 600, 450, 720], fill=W)
d.ellipse([570, 540, 730, 660], fill=W)
d.rectangle([410, 300, 450, 670], fill=W)
d.rectangle([690, 240, 730, 610], fill=W)
d.polygon([(410, 300), (730, 240), (730, 330), (410, 390)], fill=W)
img.save("build_assets/icon.png")
