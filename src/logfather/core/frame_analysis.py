"""Frame-analysis maths: pixel differencing and optical-flow views.

Pure numpy/OpenCV functions extracted from replay_view (they were also
byte-identical copies of tools/Vid_Frame_Differencing.py's helpers).
No Qt, no state.
"""
from __future__ import annotations

import cv2
import numpy as np

# -------- Frame analysis helpers (diff + optical flow) --------

def _resize_for_compute(img: np.ndarray, scale: float) -> np.ndarray:
    if scale >= 0.999:
        return img
    h, w = img.shape[:2]
    nw = max(2, int(w * scale))
    nh = max(2, int(h * scale))
    return cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA)


def _upscale_to(img: np.ndarray, target_shape_hw: tuple[int, int]) -> np.ndarray:
    th, tw = target_shape_hw
    h, w = img.shape[:2]
    if (h, w) == (th, tw):
        return img
    return cv2.resize(img, (tw, th), interpolation=cv2.INTER_NEAREST)


def compute_pixel_diff_view(
    frame_rgb: np.ndarray,
    base_rgb: np.ndarray,
    gain: float,
    threshold: int,
    heatmap: bool,
    overlay: bool,
    alpha: float,
) -> np.ndarray:
    diff = cv2.absdiff(frame_rgb, base_rgb)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_RGB2GRAY)

    d = diff_gray.astype(np.float32) * float(gain)
    d = np.clip(d, 0, 255).astype(np.uint8)

    if threshold > 0:
        _, d = cv2.threshold(d, threshold, 255, cv2.THRESH_TOZERO)

    if heatmap:
        colored = cv2.applyColorMap(d, cv2.COLORMAP_TURBO)  # BGR
        colored = cv2.cvtColor(colored, cv2.COLOR_BGR2RGB)
        if overlay:
            return cv2.addWeighted(frame_rgb, 1.0 - alpha, colored, alpha, 0.0)
        return colored

    gray_rgb = cv2.cvtColor(d, cv2.COLOR_GRAY2RGB)
    if overlay:
        return cv2.addWeighted(frame_rgb, 1.0 - alpha, gray_rgb, alpha, 0.0)
    return gray_rgb


def draw_flow_arrows(
    canvas_rgb: np.ndarray,
    flow: np.ndarray,
    step: int = 20,
    scale: float = 1.0,
    max_arrows: int | None = None,
    min_magnitude: float | None = None,
) -> np.ndarray:
    out = canvas_rgb.copy()
    h, w = flow.shape[:2]

    count = 0
    for y in range(step // 2, h, step):
        for x in range(step // 2, w, step):
            dx, dy = flow[y, x]
            if min_magnitude is not None and (dx * dx + dy * dy) <= (min_magnitude * min_magnitude):
                continue
            x2 = int(round(x + dx * scale))
            y2 = int(round(y + dy * scale))
            x2 = max(0, min(w - 1, x2))
            y2 = max(0, min(h - 1, y2))

            cv2.arrowedLine(out, (x, y), (x2, y2), (255, 255, 255), 1, tipLength=0.35)

            count += 1
            if max_arrows is not None and max_arrows > 0 and count >= max_arrows:
                return out
    return out


def compute_optical_flow_view(
    frame_rgb: np.ndarray,
    base_rgb: np.ndarray,
    gain: float,
    min_motion: int,
    heatmap: bool,
    overlay: bool,
    alpha: float,
    arrows: bool,
    arrow_step: int,
    arrow_scale: float,
    compute_scale: float,
    arrow_min_mag: float | None = None,
) -> np.ndarray:
    base_gray = cv2.cvtColor(base_rgb, cv2.COLOR_RGB2GRAY)
    frame_gray = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2GRAY)

    base_small = _resize_for_compute(base_gray, compute_scale)
    frame_small = _resize_for_compute(frame_gray, compute_scale)

    flow = cv2.calcOpticalFlowFarneback(
        base_small,
        frame_small,
        None,
        pyr_scale=0.5,
        levels=3,
        winsize=15,
        iterations=3,
        poly_n=5,
        poly_sigma=1.2,
        flags=0,
    )

    mag, _ang = cv2.cartToPolar(flow[..., 0], flow[..., 1], angleInDegrees=False)
    m = mag.astype(np.float32) * float(gain)
    m = np.clip(m, 0.0, 255.0).astype(np.uint8)

    if min_motion > 0:
        _, m = cv2.threshold(m, min_motion, 255, cv2.THRESH_TOZERO)

    if heatmap:
        vis = cv2.applyColorMap(m, cv2.COLORMAP_TURBO)  # BGR
        vis = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
    else:
        vis = cv2.cvtColor(m, cv2.COLOR_GRAY2RGB)

    H, W = frame_rgb.shape[:2]
    if compute_scale < 0.999:
        vis = _upscale_to(vis, (H, W))
        flow_up = cv2.resize(flow, (W, H), interpolation=cv2.INTER_NEAREST).astype(np.float32)
        flow_up[..., 0] *= (1.0 / compute_scale)
        flow_up[..., 1] *= (1.0 / compute_scale)
    else:
        flow_up = flow

    if arrows:
        vis = draw_flow_arrows(
            vis,
            flow_up,
            step=arrow_step,
            scale=arrow_scale,
            max_arrows=None,
            min_magnitude=arrow_min_mag,
        )

    if overlay:
        return cv2.addWeighted(frame_rgb, 1.0 - alpha, vis, alpha, 0.0)

    return vis

