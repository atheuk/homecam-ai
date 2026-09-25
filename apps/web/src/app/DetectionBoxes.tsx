"use client";
/** Borders drawn around whatever the detector actually saw.
 *
 * A cropped security photo answers "something happened"; a border answers
 * "*that* is the thing that happened", which matters most when the frame
 * also contains a parked car, a bush moving, or a second person.
 *
 * The coordinates are supplied by the API already expressed against the
 * stored photo (see ``app/ai/best_photo.py::overlay_boxes``), because the
 * photo is a crop of the source frame and the detector's own coordinates
 * are relative to the *whole* frame. Nothing here re-derives geometry: it
 * only turns 0-1 numbers into percentages, so the same markup stays correct
 * at thumbnail size and at 8x zoom.
 */

export type DetectionBox = {
  label: string;
  confidence: number;
  clipped?: boolean;
  box: {x1: number; y1: number; x2: number; y2: number};
};

function titleCase(label: string) {
  return label.charAt(0).toUpperCase() + label.slice(1);
}

/** Text shown on a border, e.g. "Sarah 93%" or "Dog 88%". */
export function boxLabel(box: DetectionBox, name?: string | null) {
  const who = name?.trim() || titleCase(box.label);
  const confidence = Math.round((box.confidence ?? 0) * 100);
  return confidence > 0 ? `${who} ${confidence}%` : who;
}

/** Absolutely-positioned borders over a photo.
 *
 * Must be rendered inside an element that wraps the image *exactly*; a
 * container that letterboxes or crops the image (``object-fit: cover``)
 * would shift every border off its subject.
 */
export function DetectionBoxes({
  boxes,
  name,
}: {
  boxes?: DetectionBox[] | null;
  /** Identity for the subject, used only when there is exactly one thing
   * in shot -- labelling two strangers with one name would be a lie. */
  name?: string | null;
}) {
  if (!boxes?.length) return null;
  const single = boxes.length === 1;
  return (
    <span className="detection-boxes">
      {boxes.map((box, index) => {
        const {x1, y1, x2, y2} = box.box;
        return (
          <span
            key={`${box.label}-${index}`}
            className={box.clipped ? "detection-box clipped" : "detection-box"}
            style={{
              left: `${x1 * 100}%`,
              top: `${y1 * 100}%`,
              width: `${(x2 - x1) * 100}%`,
              height: `${(y2 - y1) * 100}%`,
            }}
          >
            <span className="detection-tag">{boxLabel(box, single ? name : null)}</span>
          </span>
        );
      })}
    </span>
  );
}
