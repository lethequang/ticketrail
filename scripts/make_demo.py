"""Build docs/images/demo.gif (and the two README screenshots) from the real picker on synthetic data.

    uv run --no-project --with textual==8.2.8 --with pillow python scripts/make_demo.py

macOS only: SVG frames are rasterised with `qlmanage` (Quick Look). No real session data is involved.
"""
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "tests"), os.path.join(ROOT, "src")]

from helpers import NOW, Sandbox, TEST_CONFIG, mk  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from ticketrail.model import Model  # noqa: E402
from ticketrail.tui import app as P  # noqa: E402

IMAGES = os.path.join(ROOT, "docs", "images")
SIZE = (120, 32)
WIDTH = 1400  # px of every frame


def build():
    sb = Sandbox(dict(TEST_CONFIG), root_name="shop")
    root = sb.root
    os.makedirs(root + "/docs")
    os.makedirs(root + "/src/checkout")
    for rel, text in (("/src/checkout/retry.py", "# retry with backoff\n"), ("/docs/gateway.md", "# Payment gateway\n"),
                      ("/docs/onboarding.md", "# Onboarding\n")):
        with open(root + rel, "w") as f:  # files the sessions wrote exist, so links are not struck through
            f.write(text)
    with open(root + "/docs/CHECKOUT_CHANGELOG.md", "w") as f:
        f.write("# Checkout\n\n## 2026-10-12 — Retry on 502\n\n- Retry with exponential backoff (3 attempts) on 502/503 from the "
                "payment gateway.\n- Idempotency key on every attempt.\n- Waiting for QA retest on staging.\n\nSession: aaaa1111\n")

    def s(sid, tk, age, prompts, title=None, files=(), kb=300, tool="claude", branches=()):
        d = mk(sid, tk, title_custom=title, mtime=NOW - age, size_kb=kb, files=[root + f for f in files], tool=tool,
               prompts=[{"ts": "2026-10-1%dT0%d:00:00Z" % (i % 5, 2 + i % 7), "text": p} for i, p in enumerate(prompts)])
        d["title_ai"] = prompts[0][:40]
        d["branches"] = list(branches)
        d["last_assistant"] = "Payment retry is in place; unit tests pass.\nWaiting for QA to retest on staging."
        return d
    ss = [
        s("aaaa1111-0000-4000-8000-000000000001", {"ABC-142": 6}, 300, ["Checkout fails with 502 on ABC-142", "add retry with backoff",
          "write tests", "update the changelog"], "ABC-142 checkout retry", ["/src/checkout/retry.py", "/docs/CHECKOUT_CHANGELOG.md"],
          6200, branches=["feature/ABC-142-retry"]),
        s("aaaa2222-0000-4000-8000-000000000002", {"ABC-142": 2}, 86400 * 2, ["Investigate ABC-142 gateway timeouts"], None,
          ["/docs/gateway.md"]),
        s("aaaa3333-0000-4000-8000-000000000003", {"ABC-142": 1}, 86400 * 3, ["Reproduce ABC-142 locally"], tool="codex"),
        s("bbbb1111-0000-4000-8000-000000000004", {"ABC-150": 3}, 3600 * 5, ["Search results empty for accented queries ABC-150"],
          tool="codex"),
        s("cccc1111-0000-4000-8000-000000000005", {}, 3600 * 26, ["Why is the dev server slow on startup?"], "OPS dev server slow"),
        s("dddd1111-0000-4000-8000-000000000006", {"AB-77": 1}, 86400 * 6, ["Write the onboarding guide AB-77"], None,
          ["/docs/onboarding.md"], branches=["docs/AB-77-onboarding"]),
    ]
    lb = lambda topic, title, role, status, summary, nxt="": {"topic": topic, "title": title, "role": role, "status": status,  # noqa: E731
                                                              "summary": summary, "next": nxt, "v": 1}
    labels = {
        ss[0]["id"]: lb("checkout-retry", "Checkout retry", "code", "waiting",
                        "Added retry with backoff for 502s from the payment gateway; tests pass.", "QA retest on staging"),
        ss[1]["id"]: lb("checkout-retry", "Checkout retry", "research", "done", "Gateway times out after 30 s under load; root cause found."),
        ss[2]["id"]: lb("checkout-retry", "Checkout retry", "test", "done", "Reproduced the 502 with a local proxy."),
        ss[3]["id"]: lb("search-accents", "Accent-insensitive search", "code", "in_progress",
                        "Normalising queries to NFC; index rebuild pending.", "rebuild the search index"),
        ss[4]["id"]: lb("dev-server-startup", "Dev server startup", "env", "abandoned", "Profiling the dev server start."),
        ss[5]["id"]: lb("onboarding-guide", "Onboarding guide", "docs", "in_progress", "Wrote the onboarding guide.", "review"),
    }
    prs = {"heads": {"feature/ABC-142-retry": [{"number": 318, "url": "https://github.com/acme/shop/pull/318", "state": "OPEN", "mergedAt": None}],
                     "docs/AB-77-onboarding": [{"number": 301, "url": "https://github.com/acme/shop/pull/301", "state": "MERGED", "mergedAt": None}]},
           "bases": ["main"]}
    model = Model(no_llm=True, load_sessions=lambda: [dict(d) for d in ss], load_labels=lambda: labels,
                  load_live=lambda: {ss[0]["id"]: "open"}, load_prs=lambda: prs)
    return sb, model


# (keys to press before the frame, caption, seconds on screen)
SCRIPT = [
    ([], "Work items, one per ticket — with the status of the work and why", 3.2),
    (["down"], "ABC-150: in progress, and what is left", 1.8),
    (["down", "down"], "AB-77: done — confirmed by a merged pull request", 2.2),
    (["up", "up", "up"], "Back to ABC-142: waiting on QA. Docs, PR and history in one place", 2.8),
    (["right"], "→ its sessions (Claude and Codex). The open one is waiting for you (◐)", 2.6),
    (["o"], "o: everything the work left behind", 2.2),
    (["down", "down", "down", "enter"], "Enter: preview the changelog entry that cites this session", 2.8),
    (["escape", "escape", "left"], "", 0.8),
    (["n"], "n: hand the work off to a new session…", 2.2),
    ([], "…Codex or Claude — its first prompt already lists every earlier session", 3.0),
]


async def capture(tmp):
    sb, model = build()
    frames = []
    try:
        app = P.Picker(model, "")
        app.open_url = lambda *a, **k: None
        with __import__("unittest.mock").mock.patch.object(P.shutil, "which", lambda c: "/bin/" + c):
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause(0.4)
                app.save_screenshot(os.path.join(IMAGES, "picker-group.svg"))
                for i, (keys, caption, secs) in enumerate(SCRIPT):
                    for k in keys:
                        await pilot.press(k)
                        await pilot.pause(0.15)
                    await pilot.pause(0.3)
                    path = os.path.join(tmp, "f%02d.svg" % i)
                    app.save_screenshot(path)
                    frames.append((path, caption, secs))
                    if keys == ["right"]:
                        app.save_screenshot(os.path.join(IMAGES, "picker-session.svg"))
    finally:
        sb.close()
    return frames


def rasterise(svg, tmp):
    subprocess.run(["qlmanage", "-t", "-s", str(WIDTH), "-o", tmp, svg], check=True, capture_output=True)
    img = Image.open(svg if svg.endswith(".png") else os.path.join(tmp, os.path.basename(svg) + ".png")).convert("RGB")
    # qlmanage pads to a square thumbnail: crop to the terminal window
    bg = img.getpixel((2, 2))
    mask = Image.eval(img.convert("L"), lambda v: 255 if abs(v - sum(bg) // 3) > 8 else 0)
    box = mask.getbbox()
    return img.crop(box) if box else img


def caption_bar(img, text):
    bar_h = 56
    out = Image.new("RGB", (img.width, img.height + bar_h), (24, 24, 27))
    out.paste(img, (0, 0))
    if text:
        draw = ImageDraw.Draw(out)
        font = None
        for f in ("/System/Library/Fonts/SFNS.ttf", "/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Arial.ttf"):
            if os.path.exists(f):
                font = ImageFont.truetype(f, 26)
                break
        draw.text((img.width // 2, img.height + bar_h // 2), text, fill=(240, 240, 240), font=font, anchor="mm")
    return out


def main():
    tmp = tempfile.mkdtemp()
    try:
        frames = asyncio.run(capture(tmp))
        imgs = [caption_bar(rasterise(p, tmp), c) for p, c, _ in frames]
        w = min(i.width for i in imgs)
        h = min(i.height for i in imgs)
        imgs = [i.crop((0, 0, w, h)).resize((1100, int(h * 1100 / w)), Image.LANCZOS) for i in imgs]
        pal = [i.convert("P", palette=Image.ADAPTIVE, colors=128) for i in imgs]
        out = os.path.join(IMAGES, "demo.gif")
        pal[0].save(out, save_all=True, append_images=pal[1:], duration=[int(s * 1000) for _, _, s in frames], loop=0,
                    optimize=True, disposal=1)
        print("wrote %s (%.1f MB, %d frames, %.0f s)" % (out, os.path.getsize(out) / 1e6, len(pal), sum(s for _, _, s in frames)))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
