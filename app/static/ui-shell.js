/*! Icons from lucide-static 0.468.0 (https://lucide.dev/)

ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

Feather-derived icons (https://feathericons.com/):

The MIT License (MIT)

Copyright (c) 2013-2023 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

/* Local UI helpers for fixed popovers mounted outside transformed containers. */
(function (root) {
  "use strict";

  const ICO = Object.freeze({
    plus: '<path d="M5 12h14" /><path d="M12 5v14" />',
    search: '<circle cx="11" cy="11" r="8" /><path d="m21 21-4.3-4.3" />',
    folder: '<path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />',
    chat: '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />',
    pin: '<path d="M12 17v5" /><path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />',
    more: '<circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /><circle cx="5" cy="12" r="1" />',
    settings: '<path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" /><circle cx="12" cy="12" r="3" />',
    edit: '<path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" /><path d="m15 5 4 4" />',
    trash: '<path d="M3 6h18" /><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6" /><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2" /><line x1="10" x2="10" y1="11" y2="17" /><line x1="14" x2="14" y1="11" y2="17" />',
    archive: '<rect width="20" height="5" x="2" y="3" rx="1" /><path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" /><path d="M10 12h4" />',
    share: '<circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><line x1="8.59" x2="15.42" y1="13.51" y2="17.49" /><line x1="15.41" x2="8.59" y1="6.51" y2="10.49" />',
    terminal: '<polyline points="4 17 10 11 4 5" /><line x1="12" x2="20" y1="19" y2="19" />',
    cpu: '<rect width="16" height="16" x="4" y="4" rx="2" /><rect width="6" height="6" x="9" y="9" rx="1" /><path d="M15 2v2" /><path d="M15 20v2" /><path d="M2 15h2" /><path d="M2 9h2" /><path d="M20 15h2" /><path d="M20 9h2" /><path d="M9 2v2" /><path d="M9 20v2" />',
    check: '<path d="M20 6 9 17l-5-5" />',
    x: '<path d="M18 6 6 18" /><path d="m6 6 12 12" />',
    chevronDown: '<path d="m6 9 6 6 6-6" />',
    chevronRight: '<path d="m9 18 6-6-6-6" />',
    sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z" /><path d="M20 3v4" /><path d="M22 5h-4" /><path d="M4 17v2" /><path d="M5 18H3" />',
    layers: '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z" /><path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12" /><path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17" />',
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" x2="12" y1="19" y2="22" />',
    send: '<path d="M14.536 21.686a.5.5 0 0 0 .937-.024l6.5-19a.496.496 0 0 0-.635-.635l-19 6.5a.5.5 0 0 0-.024.937l7.93 3.18a2 2 0 0 1 1.112 1.11z" /><path d="m21.854 2.147-10.94 10.939" />',
    paperclip: '<path d="M13.234 20.252 21 12.3" /><path d="m16 6-8.414 8.586a2 2 0 0 0 0 2.828 2 2 0 0 0 2.828 0l8.414-8.586a4 4 0 0 0 0-5.656 4 4 0 0 0-5.656 0l-8.415 8.585a6 6 0 1 0 8.486 8.486" />',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8" /><path d="M21 3v5h-5" /><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16" /><path d="M8 16H3v5" />',
    gitBranch: '<line x1="6" x2="6" y1="3" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" />',
    history: '<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" /><path d="M3 3v5h5" /><path d="M12 7v5l4 2" />',
    shieldCheck: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" /><path d="m9 12 2 2 4-4" />',
    cloud: '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z" />',
    lock: '<rect width="18" height="11" x="3" y="11" rx="2" ry="2" /><path d="M7 11V7a5 5 0 0 1 10 0v4" />',
    zeroRing: '<path d="M20.37 10.52a8.5 8.5 0 1 1-6.89-6.89" /><circle cx="18.01" cy="5.99" r="1.15" fill="currentColor" stroke="none" /><circle cx="12" cy="12" r="2" fill="currentColor" stroke="none" />',
    eye: '<path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0" /><circle cx="12" cy="12" r="3" />',
    eyeOff: '<path d="M10.733 5.076a10.744 10.744 0 0 1 11.205 6.575 1 1 0 0 1 0 .696 10.747 10.747 0 0 1-1.444 2.49" /><path d="M14.084 14.158a3 3 0 0 1-4.242-4.242" /><path d="M17.479 17.499a10.75 10.75 0 0 1-15.417-5.151 1 1 0 0 1 0-.696 10.75 10.75 0 0 1 4.446-5.143" /><path d="m2 2 20 20" />',
    copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />',
    key: '<path d="m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4" /><path d="m21 2-9.6 9.6" /><circle cx="7.5" cy="15.5" r="5.5" />',
  });

  function icon(name, cls = "") {
    if (!Object.hasOwn(ICO, name)) return "";
    const classes = String(cls).trim().split(/\s+/).filter(token => /^[\w-]+$/.test(token));
    return `<svg class="${["ui-ico", ...classes].join(" ")}" xmlns="http://www.w3.org/2000/svg" ` +
      `viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" ` +
      `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICO[name]}</svg>`;
  }

  function viewportBounds() {
    const viewport = root.visualViewport;
    return {
      left: viewport?.offsetLeft ?? 0,
      top: viewport?.offsetTop ?? 0,
      width: viewport?.width ?? root.innerWidth,
      height: viewport?.height ?? root.innerHeight,
    };
  }

  function rect(value) {
    return value.getBoundingClientRect?.() ?? value;
  }

  function clamp(value, min, max) {
    return Math.min(Math.max(value, min), max);
  }

  function computeFloatingPosition(anchor, floating, {
    placement = "bottom-start", offset = 6, padding = 8, matchMinWidth = false,
    viewport = viewportBounds(),
  } = {}) {
    const reference = rect(anchor);
    const size = rect(floating);
    const insetX = clamp(padding, 0, viewport.width / 2);
    const insetY = clamp(padding, 0, viewport.height / 2);
    const left = (viewport.left ?? 0) + insetX;
    const top = (viewport.top ?? 0) + insetY;
    const maxWidth = viewport.width - insetX * 2;
    const viewportHeight = viewport.height - insetY * 2;
    const right = left + maxWidth;
    const bottom = top + viewportHeight;
    const anchorRight = reference.right ?? reference.left + reference.width;
    const anchorBottom = reference.bottom ?? reference.top + reference.height;
    const anchorWidth = reference.width ?? anchorRight - reference.left;
    const minWidth = matchMinWidth ? Math.min(anchorWidth, maxWidth) : 0;
    const width = clamp(size.width, minWidth, maxWidth);

    let [side, align] = placement.split("-");
    const above = clamp(reference.top - offset, top, bottom);
    const below = clamp(anchorBottom + offset, top, bottom);
    const room = { top: above - top, bottom: bottom - below };
    const opposite = side === "top" ? "bottom" : "top";
    if (size.height > room[side] && room[opposite] > room[side]) side = opposite;

    const maxHeight = room[side];
    const height = Math.min(size.height, maxHeight);
    const x = clamp(align === "end" ? anchorRight - width : reference.left, left, right - width);
    const y = side === "top" ? above - height : below;
    return { x, y, placement: `${side}-${align}`, maxHeight, maxWidth, minWidth };
  }

  // Open the popover before placing it so it can be measured.
  function positionPopover(anchor, popover, options = {}) {
    const viewport = options.viewport ?? viewportBounds();
    const constraints = { ...options, viewport };
    const reference = rect(anchor);
    const { maxWidth, minWidth } = computeFloatingPosition(reference, { width: 0, height: 0 }, constraints);
    const scrollTop = popover.scrollTop;
    Object.assign(popover.style, {
      position: "fixed",
      boxSizing: "border-box",
      left: `${(viewport.left ?? 0) + (viewport.width - maxWidth) / 2}px`,
      right: "auto",
      bottom: "auto",
      minWidth: `${minWidth}px`,
      maxWidth: `${maxWidth}px`,
      minHeight: "0",
      maxHeight: "none",
      overflow: "auto",
    });
    // Measure after constraining width, since wrapping changes the natural height.
    const result = computeFloatingPosition(reference, popover, constraints);
    Object.assign(popover.style, {
      left: `${result.x}px`,
      top: `${result.y}px`,
      maxHeight: `${result.maxHeight}px`,
    });
    if (scrollTop) popover.scrollTop = scrollTop;
    return result;
  }

  const openPopovers = new Map();
  let stopListening;

  function unbind(entry) {
    if (openPopovers.get(entry.popover) !== entry) return;
    openPopovers.delete(entry.popover);
    if (!openPopovers.size) {
      stopListening();
      stopListening = undefined;
    }
  }

  function dismiss(entry, reason) {
    unbind(entry);
    entry.popover.hidden = true;
    if (reason === "escape") entry.anchor.focus?.();
    entry.onDismiss?.(reason);
  }

  function closeOpenPopovers() {
    for (const entry of [...openPopovers.values()].reverse()) {
      if (openPopovers.get(entry.popover) === entry) dismiss(entry, "programmatic");
    }
  }

  function listen() {
    const doc = root.document;
    const viewport = root.visualViewport;
    const update = (event) => {
      const scrolled = event.type === "scroll" && event.target instanceof root.Node ? event.target : null;
      for (const entry of openPopovers.values()) {
        if (entry.popover.hidden || entry.popover.isConnected === false || entry.anchor.isConnected === false) {
          unbind(entry);
        } else if (!scrolled || !entry.popover.contains(scrolled)) {
          positionPopover(entry.anchor, entry.popover, entry.options);
        }
      }
    };
    const onKey = (event) => {
      if (event.key !== "Escape" || event.defaultPrevented) return;
      const entry = [...openPopovers.values()].at(-1);
      if (!entry) return;
      event.preventDefault();
      dismiss(entry, "escape");
    };
    const onPointer = (event) => {
      const inside = (element) =>
        event.composedPath?.().includes(element) || element.contains?.(event.target);
      for (const entry of [...openPopovers.values()].reverse()) {
        if (inside(entry.popover) || inside(entry.anchor)) break;
        if (openPopovers.get(entry.popover) === entry) dismiss(entry, "outside");
      }
    };
    doc.addEventListener("keydown", onKey);
    doc.addEventListener("pointerdown", onPointer, true);
    root.addEventListener("resize", update);
    root.addEventListener("scroll", update, true);
    viewport?.addEventListener("resize", update);
    viewport?.addEventListener("scroll", update);
    return () => {
      doc.removeEventListener("keydown", onKey);
      doc.removeEventListener("pointerdown", onPointer, true);
      root.removeEventListener("resize", update);
      root.removeEventListener("scroll", update, true);
      viewport?.removeEventListener("resize", update);
      viewport?.removeEventListener("scroll", update);
    };
  }

  // Binds a visible popover. The returned cleanup removes listeners without
  // hiding it. Dismissal hides it and reports the reason to the caller.
  function bindDismissablePopover(anchor, popover, { onDismiss, ...options } = {}) {
    positionPopover(anchor, popover, options);
    if (!openPopovers.size) stopListening = listen();
    const entry = { anchor, popover, options, onDismiss };
    openPopovers.delete(popover);
    openPopovers.set(popover, entry);
    return () => unbind(entry);
  }

  let activeConfirmDismiss = null;

  function confirm({
    title = "Подтвердите действие",
    message = "",
    confirmText = "Подтвердить",
    cancelText = "Отмена",
    danger = false,
  } = {}) {
    const doc = root.document;
    const modal = doc?.getElementById?.("agyConfirmModal") ?? null;
    const titleEl = doc?.getElementById?.("agyConfirmTitle") ?? null;
    const msgEl = doc?.getElementById?.("agyConfirmMessage") ?? null;
    const cancelBtn = doc?.getElementById?.("agyConfirmCancelBtn") ?? null;
    const okBtn = doc?.getElementById?.("agyConfirmOkBtn") ?? null;
    if (!modal || !okBtn || !cancelBtn) {
      const text = String(message || title || "");
      return Promise.resolve(typeof root.confirm === "function" ? Boolean(root.confirm(text)) : false);
    }

    if (typeof activeConfirmDismiss === "function") {
      activeConfirmDismiss(false);
    }

    return new Promise((resolve) => {
      let settled = false;
      const prevFocus = doc.activeElement ?? null;

      if (titleEl) titleEl.textContent = String(title || "Подтвердите действие");
      if (msgEl) msgEl.textContent = String(message || "");
      cancelBtn.textContent = String(cancelText || "Отмена");
      okBtn.textContent = String(confirmText || "Подтвердить");

      okBtn.classList?.toggle?.("btn-danger", Boolean(danger));
      okBtn.classList?.toggle?.("ui-btn-danger", Boolean(danger));
      okBtn.classList?.toggle?.("btn-primary", !danger);

      modal.classList?.remove?.("hidden");
      modal.removeAttribute?.("hidden");

      const finish = (result) => {
        if (settled) return;
        settled = true;
        if (activeConfirmDismiss === finish) activeConfirmDismiss = null;
        okBtn.removeEventListener?.("click", onOk);
        cancelBtn.removeEventListener?.("click", onCancel);
        modal.removeEventListener?.("click", onBackdrop);
        doc.removeEventListener?.("keydown", onKeyDown, true);
        modal.classList?.add?.("hidden");
        if (prevFocus && prevFocus.isConnected !== false) {
          try { prevFocus.focus?.(); } catch {}
        }
        resolve(Boolean(result));
      };

      const onOk = (e) => {
        e?.preventDefault?.();
        e?.stopPropagation?.();
        finish(true);
      };
      const onCancel = (e) => {
        e?.preventDefault?.();
        e?.stopPropagation?.();
        finish(false);
      };
      const onBackdrop = (e) => {
        if (e?.target !== modal) return;
        e.preventDefault?.();
        e.stopPropagation?.();
        finish(false);
      };
      const onKeyDown = (e) => {
        if (!e) return;
        if (e.key === "Escape") {
          e.preventDefault?.();
          e.stopPropagation?.();
          e.stopImmediatePropagation?.();
          finish(false);
          return;
        }
        if (e.key === "Enter") {
          const active = doc.activeElement;
          if (active === okBtn) {
            e.preventDefault?.();
            e.stopPropagation?.();
            e.stopImmediatePropagation?.();
            finish(true);
          } else if (active === cancelBtn) {
            e.preventDefault?.();
            e.stopPropagation?.();
            e.stopImmediatePropagation?.();
            finish(false);
          }
          return;
        }
        if (e.key === "Tab") {
          const active = doc.activeElement;
          if (e.shiftKey) {
            if (active === cancelBtn || (active !== okBtn && active !== cancelBtn)) {
              e.preventDefault?.();
              okBtn.focus?.();
            }
          } else if (active === okBtn || (active !== okBtn && active !== cancelBtn)) {
            e.preventDefault?.();
            cancelBtn.focus?.();
          }
        }
      };

      activeConfirmDismiss = finish;
      okBtn.addEventListener?.("click", onOk);
      cancelBtn.addEventListener?.("click", onCancel);
      modal.addEventListener?.("click", onBackdrop);
      doc.addEventListener?.("keydown", onKeyDown, true);

      try {
        (danger ? cancelBtn : okBtn).focus?.();
      } catch {}
    });
  }

  root.AgyUI = Object.freeze({ computeFloatingPosition, positionPopover, bindDismissablePopover, closeOpenPopovers, confirm, ICO, icon });
})(globalThis);
