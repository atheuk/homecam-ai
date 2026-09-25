"use client";
/** Full-screen photo viewer with zoom and pan.
 *
 * Detection photos are deliberately small server-side crops, so on the
 * event list they are often too small to answer the only question that
 * matters: "who is that?". This gives every stored photo a way to be
 * opened full screen and inspected closely, without leaving the app or
 * downloading the file.
 *
 * Zoom is anchored to wherever the user is pointing (wheel, pinch or
 * double-click) rather than to the middle of the screen. Anchoring to the
 * centre forces a zoom-then-drag-then-zoom dance to reach a face near an
 * edge, which is exactly the case this exists for.
 */
import {useCallback,useEffect,useRef,useState} from "react";
import {createPortal} from "react-dom";

const MIN_SCALE = 1;
const MAX_SCALE = 8;
const ZOOM_STEP = 1.4;
const KEYBOARD_PAN_PX = 60;

type Point = {x: number; y: number};
/** Scale plus the pan offset in screen pixels, from the viewport centre. */
type View = Point & {scale: number};

const RESET: View = {scale: 1, x: 0, y: 0};

export type LightboxPhoto = {src: string; alt: string; caption?: string | null; title?: string};

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

function distanceBetween(a: Point, b: Point) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

export function Lightbox({photo, onClose}: {photo: LightboxPhoto; onClose: () => void}) {
  const [view, setView] = useState<View>(RESET);
  const viewportRef = useRef<HTMLDivElement | null>(null);
  const imageRef = useRef<HTMLImageElement | null>(null);
  const dialogRef = useRef<HTMLDivElement | null>(null);
  // Native wheel/pointer handlers close over the view, so they need a ref
  // to read the current one rather than the one from their render.
  const viewRef = useRef(view);
  viewRef.current = view;
  const pointers = useRef(new Map<number, Point>());
  const pinch = useRef<{distance: number; scale: number} | null>(null);
  // Distinguishes a pan that happens to end on the backdrop from a click
  // meant to dismiss, so dragging the photo never closes the viewer.
  const panned = useRef(false);

  /** Keep the photo's own edges from being dragged inside the viewport.
   *
   * Once an axis is smaller than the viewport there is no slack on it, so
   * it stays centred -- that is what makes a zoomed-out photo snap back
   * instead of drifting into a corner. */
  const clampOffset = useCallback((next: Point, atScale: number): Point => {
    const viewport = viewportRef.current;
    const image = imageRef.current;
    if (!viewport || !image) return next;
    const slackX = Math.max(0, (image.clientWidth * atScale - viewport.clientWidth) / 2);
    const slackY = Math.max(0, (image.clientHeight * atScale - viewport.clientHeight) / 2);
    return {x: clamp(next.x, -slackX, slackX), y: clamp(next.y, -slackY, slackY)};
  }, []);

  /** Zoom to `target`, keeping whatever is under `focus` under `focus`.
   * `focus` is relative to the viewport centre, matching the transform
   * origin used when rendering. */
  const zoomTo = useCallback(
    (target: number, focus: Point = {x: 0, y: 0}) => {
      setView(current => {
        const scale = clamp(target, MIN_SCALE, MAX_SCALE);
        if (scale === current.scale) return current;
        if (scale === MIN_SCALE) return RESET;
        const ratio = scale / current.scale;
        const moved = {x: focus.x - (focus.x - current.x) * ratio, y: focus.y - (focus.y - current.y) * ratio};
        return {scale, ...clampOffset(moved, scale)};
      });
    },
    [clampOffset],
  );

  const panBy = useCallback(
    (dx: number, dy: number) => {
      setView(current => ({...current, ...clampOffset({x: current.x + dx, y: current.y + dy}, current.scale)}));
    },
    [clampOffset],
  );

  /** Pointer position relative to the viewport centre. */
  const focusFor = useCallback((clientX: number, clientY: number): Point => {
    const rect = viewportRef.current?.getBoundingClientRect();
    if (!rect) return {x: 0, y: 0};
    return {x: clientX - rect.left - rect.width / 2, y: clientY - rect.top - rect.height / 2};
  }, []);

  // Wheel must be a non-passive native listener: React attaches wheel
  // passively, so preventDefault there is ignored and zooming would scroll
  // the page behind the viewer instead.
  useEffect(() => {
    const viewport = viewportRef.current;
    if (!viewport) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const direction = event.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP;
      zoomTo(viewRef.current.scale * direction, focusFor(event.clientX, event.clientY));
    };
    viewport.addEventListener("wheel", onWheel, {passive: false});
    return () => viewport.removeEventListener("wheel", onWheel);
  }, [zoomTo, focusFor]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") return onClose();
      if (event.key === "+" || event.key === "=") zoomTo(viewRef.current.scale * ZOOM_STEP);
      else if (event.key === "-" || event.key === "_") zoomTo(viewRef.current.scale / ZOOM_STEP);
      else if (event.key === "0") setView(RESET);
      else if (event.key === "ArrowLeft") panBy(KEYBOARD_PAN_PX, 0);
      else if (event.key === "ArrowRight") panBy(-KEYBOARD_PAN_PX, 0);
      else if (event.key === "ArrowUp") panBy(0, KEYBOARD_PAN_PX);
      else if (event.key === "ArrowDown") panBy(0, -KEYBOARD_PAN_PX);
      else return;
      event.preventDefault();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose, zoomTo, panBy]);

  // Hold the page still behind the viewer, and hand focus back to whatever
  // opened it so keyboard users do not land at the top of the document.
  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    const previouslyFocused = document.activeElement as HTMLElement | null;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previouslyFocused?.focus?.();
    };
  }, []);

  const onPointerDown = (event: React.PointerEvent) => {
    pointers.current.set(event.pointerId, {x: event.clientX, y: event.clientY});
    panned.current = false;
    if (pointers.current.size === 1) event.currentTarget.setPointerCapture?.(event.pointerId);
  };

  const onPointerMove = (event: React.PointerEvent) => {
    const previous = pointers.current.get(event.pointerId);
    if (!previous) return;
    const current = {x: event.clientX, y: event.clientY};
    pointers.current.set(event.pointerId, current);
    const active = [...pointers.current.values()];

    if (active.length >= 2) {
      const [a, b] = active;
      const separation = distanceBetween(a, b);
      const midpoint = {x: (a.x + b.x) / 2, y: (a.y + b.y) / 2};
      // The first move of a pinch only establishes the baseline; zooming
      // from it too would make the photo jump on touch-down.
      if (pinch.current && pinch.current.distance > 0) {
        zoomTo(pinch.current.scale * (separation / pinch.current.distance), focusFor(midpoint.x, midpoint.y));
      } else {
        pinch.current = {distance: separation, scale: viewRef.current.scale};
      }
      panned.current = true;
      return;
    }

    if (viewRef.current.scale <= MIN_SCALE) return;
    panned.current = true;
    panBy(current.x - previous.x, current.y - previous.y);
  };

  const onPointerUp = (event: React.PointerEvent) => {
    pointers.current.delete(event.pointerId);
    if (pointers.current.size < 2) pinch.current = null;
  };

  /** Double-click/tap toggles between fit and a close-up of that spot. */
  const onDoubleClick = (event: React.MouseEvent) => {
    const focus = focusFor(event.clientX, event.clientY);
    if (view.scale > MIN_SCALE) setView(RESET);
    else zoomTo(ZOOM_STEP * 2, focus);
  };

  const zoomed = view.scale > MIN_SCALE;
  const percentage = Math.round(view.scale * 100);

  return createPortal(
    <div
      className="lightbox"
      role="dialog"
      aria-modal="true"
      aria-label={photo.title || photo.alt}
      ref={dialogRef}
      tabIndex={-1}
      onClick={event => {
        if (event.target === event.currentTarget && !panned.current) onClose();
      }}
    >
      <div className="lightbox-bar">
        <span className="lightbox-title">{photo.title || photo.alt}</span>
        <div className="lightbox-tools">
          <button type="button" aria-label="Zoom out" disabled={!zoomed} onClick={() => zoomTo(view.scale / ZOOM_STEP)}>
            −
          </button>
          <span className="lightbox-zoom" aria-live="polite">
            {percentage}%
          </span>
          <button
            type="button"
            aria-label="Zoom in"
            disabled={view.scale >= MAX_SCALE}
            onClick={() => zoomTo(view.scale * ZOOM_STEP)}
          >
            +
          </button>
          <button type="button" aria-label="Reset zoom" disabled={!zoomed} onClick={() => setView(RESET)}>
            Reset
          </button>
          <button type="button" className="lightbox-close" aria-label="Close full screen" onClick={onClose}>
            ✕
          </button>
        </div>
      </div>

      <div
        className={zoomed ? "lightbox-viewport zoomed" : "lightbox-viewport"}
        ref={viewportRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        onDoubleClick={onDoubleClick}
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- next/image
            cannot be used here: the API origin is injected at deploy time,
            not build time, so it cannot be a configured remote pattern. */}
        <img
          ref={imageRef}
          src={photo.src}
          alt={photo.alt}
          draggable={false}
          style={{transform: `translate(${view.x}px, ${view.y}px) scale(${view.scale})`}}
        />
      </div>

      {photo.caption && <p className="lightbox-caption">{photo.caption}</p>}
      <p className="lightbox-hint muted">Scroll or pinch to zoom · drag to move · double-click to zoom in · Esc to close</p>
    </div>,
    document.body,
  );
}

/** A photo that opens full screen when clicked.
 *
 * Rendered as a real button so it is reachable by keyboard and announced
 * as activatable, rather than an image with a click handler bolted on.
 */
export function ZoomablePhoto({
  src,
  alt,
  caption,
  title,
}: {
  src: string;
  alt: string;
  caption?: string | null;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" className="photo-trigger" aria-label={`Open ${title || alt} full screen`} onClick={() => setOpen(true)}>
        {/* eslint-disable-next-line @next/next/no-img-element -- see Lightbox */}
        <img src={src} alt={alt} />
      </button>
      {open && <Lightbox photo={{src, alt, caption, title}} onClose={() => setOpen(false)} />}
    </>
  );
}
