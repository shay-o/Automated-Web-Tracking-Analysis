from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

import yaml
from playwright.sync_api import sync_playwright

# Injected into the page on every navigation. Sets up event listeners that
# call window.__trackerRecord({type, locator, value}) for each user action.
_RECORD_JS = r"""
(function () {
  if (window.__trackerInjected) return;
  window.__trackerInjected = true;

  const IMPLICIT_ROLES = {
    A:        'link',
    BUTTON:   'button',
    SELECT:   'combobox',
    TEXTAREA: 'textbox',
    SUMMARY:  'button',
    INPUT: {
      text: 'textbox', email: 'textbox', search: 'searchbox',
      password: 'textbox', number: 'spinbutton', tel: 'textbox',
      url: 'textbox', submit: 'button', reset: 'button', button: 'button',
      checkbox: 'checkbox', radio: 'radio',
    },
  };

  function getRole(el) {
    const explicit = el.getAttribute('role');
    if (explicit && explicit !== 'none' && explicit !== 'presentation') return explicit;
    const imp = IMPLICIT_ROLES[el.tagName];
    if (!imp) return null;
    if (typeof imp === 'object') return imp[el.type] || 'textbox';
    return imp;
  }

  function getAccessibleName(el) {
    // 1. aria-label
    const al = el.getAttribute('aria-label');
    if (al && al.trim()) return al.trim();
    // 2. aria-labelledby
    const alby = el.getAttribute('aria-labelledby');
    if (alby) {
      const parts = alby.split(/\s+/).map(id => {
        const ref = document.getElementById(id);
        return ref ? ref.textContent.trim() : '';
      }).filter(Boolean);
      if (parts.length) return parts.join(' ');
    }
    // 3. <label for="id">
    if (el.id) {
      try {
        const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lbl) return lbl.textContent.trim();
      } catch (e) {}
    }
    // 4. placeholder
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    // 5. text content (buttons, links, short labels)
    const txt = el.textContent && el.textContent.trim();
    if (txt && txt.length > 0 && txt.length <= 80) return txt;
    // 6. title
    const title = el.getAttribute('title');
    if (title && title.trim()) return title.trim();
    return null;
  }

  function getCSSSelector(el) {
    if (el.id) {
      try { return '#' + CSS.escape(el.id); } catch (e) { return '#' + el.id; }
    }
    const parts = [];
    let cur = el;
    while (cur && cur !== document.body && cur.tagName) {
      if (cur.id) {
        try { parts.unshift('#' + CSS.escape(cur.id)); } catch (e) { parts.unshift('#' + cur.id); }
        break;
      }
      let part = cur.tagName.toLowerCase();
      const siblings = cur.parentNode
        ? Array.from(cur.parentNode.children).filter(s => s.tagName === cur.tagName)
        : [];
      if (siblings.length > 1) {
        part += ':nth-of-type(' + (siblings.indexOf(cur) + 1) + ')';
      }
      parts.unshift(part);
      cur = cur.parentElement;
    }
    return parts.join(' > ');
  }

  function buildLocator(el) {
    const role = getRole(el);
    const name = getAccessibleName(el);
    if (role && name) return { role: role, name: name };
    // No name — try text content for non-form elements
    const txt = el.textContent && el.textContent.trim();
    if (!role && txt && txt.length <= 80) return { text: txt };
    if (role) return { role: role };
    return { css: getCSSSelector(el) };
  }

  function emit(data) {
    if (typeof window.__trackerRecord === 'function') {
      window.__trackerRecord(data);
    }
  }

  // Track the currently focused input to:
  // (a) record fill on blur/Enter, (b) avoid double-counting click+fill on same element
  let activeInput = null;
  let lastFillValue = null;  // deduplicate focusout after Enter

  function isTextInput(el) {
    return (
      (el.tagName === 'INPUT' && !['submit','button','checkbox','radio','file','image','range','color'].includes(el.type)) ||
      el.tagName === 'TEXTAREA' ||
      (el.isContentEditable && el.tagName !== 'BODY' && el.tagName !== 'HTML')
    );
  }

  function inputValue(el) {
    return el.isContentEditable ? el.textContent.trim() : (el.value || '').trim();
  }

  document.addEventListener('focusin', function (e) {
    const el = e.target;
    if (isTextInput(el)) {
      activeInput = el;
      lastFillValue = null;
    } else {
      activeInput = null;
    }
  }, true);

  // Capture fill when user presses Enter (before focus might or might not leave)
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    const el = e.target;
    if (el !== activeInput) return;
    const value = inputValue(el);
    if (!value || value === lastFillValue) return;
    lastFillValue = value;
    emit({ type: 'fill', locator: buildLocator(el), value: value });
  }, true);

  // Also capture fill on blur in case user tabs away without pressing Enter
  document.addEventListener('focusout', function (e) {
    const el = e.target;
    if (el !== activeInput) return;
    const value = inputValue(el);
    if (value && value !== lastFillValue) {
      emit({ type: 'fill', locator: buildLocator(el), value: value });
    }
    activeInput = null;
    lastFillValue = null;
  }, true);

  document.addEventListener('change', function (e) {
    const el = e.target;
    if (el.tagName !== 'SELECT') return;
    const txt = el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : el.value;
    emit({ type: 'select', locator: buildLocator(el), value: txt });
  }, true);

  document.addEventListener('click', function (e) {
    // Walk up to find the nearest semantically clickable ancestor
    let el = e.target;
    let found = null;
    while (el && el !== document.body) {
      const tag = el.tagName;
      const role = el.getAttribute('role');
      const isClickable = (
        tag === 'BUTTON' || tag === 'A' || tag === 'SUMMARY' ||
        (tag === 'INPUT' && ['submit', 'button', 'checkbox', 'radio'].includes(el.type)) ||
        ['button', 'link', 'menuitem', 'menuitemcheckbox', 'menuitemradio',
         'option', 'tab', 'checkbox', 'radio', 'switch', 'treeitem'].includes(role)
      );
      if (isClickable) { found = el; break; }
      el = el.parentElement;
    }
    if (!found) return;
    // Ignore if this is the focused text input (user clicked into an input field)
    if (found === activeInput) return;
    emit({ type: 'click', locator: buildLocator(found) });
  }, true);
})();
"""

_WEBDRIVER_HIDE = "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"


def _slugify(text: str, max_len: int = 24) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:max_len]


def _make_step_id(action_type: str, locator: dict, url: str | None = None, step_num: int = 1) -> str:
    if action_type == "goto" and url:
        path = urlparse(url).path.strip("/").replace("/", "_") or "home"
        slug = _slugify(path) or "home"
        return f"load_{slug}"
    name = locator.get("name") or locator.get("text") or ""
    slug = _slugify(name)
    if slug:
        return f"{action_type}_{slug}"
    return f"step_{step_num:03d}"


def _unique_id(candidate: str, seen: set[str]) -> str:
    if candidate not in seen:
        seen.add(candidate)
        return candidate
    i = 2
    while f"{candidate}_{i}" in seen:
        i += 1
    uid = f"{candidate}_{i}"
    seen.add(uid)
    return uid


def _write_yaml(doc: dict, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Custom representer: write locator/settle as inline flow dicts for readability
    class _Dumper(yaml.Dumper):
        pass

    def _inline_dict(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=True)

    _Dumper.add_representer(dict, yaml.Dumper.represent_dict)

    # Represent locator and settle inline so steps are easy to read
    class _InlineDict(dict):
        pass

    def _repr_inline(dumper, data):
        return dumper.represent_mapping("tag:yaml.org,2002:map", data.items(), flow_style=True)

    _Dumper.add_representer(_InlineDict, _repr_inline)

    # Convert locator/settle dicts in steps to inline style
    out = dict(doc)
    out["steps"] = []
    for step in doc["steps"]:
        s = dict(step)
        if "locator" in s:
            s["locator"] = _InlineDict(s["locator"])
        if "settle" in s:
            s["settle"] = _InlineDict(s["settle"])
        out["steps"].append(s)

    with open(output_path, "w") as f:
        yaml.dump(out, f, Dumper=_Dumper, default_flow_style=False,
                  allow_unicode=True, sort_keys=False)


def record(
    start_url: str,
    output_path: Path,
    session_name: str | None = None,
    viewport_width: int = 1280,
    viewport_height: int = 800,
    default_settle_ms: int = 1500,
) -> None:
    if session_name is None:
        session_name = output_path.stem

    steps: list[dict] = []
    seen_ids: set[str] = set()

    # First step: goto the start URL
    first_id = _unique_id(_make_step_id("goto", {}, url=start_url), seen_ids)
    steps.append({
        "id": first_id,
        "action": "goto",
        "url": start_url,
        "wait_for": "load",
        "settle": {"wait_ms": default_settle_ms * 2},
    })

    def on_action(action: dict) -> None:
        atype = action.get("type", "")
        locator_raw: dict = action.get("locator") or {}
        value: str | None = action.get("value")

        locator = {k: v for k, v in locator_raw.items() if v is not None}
        step_num = len(steps) + 1
        sid = _unique_id(_make_step_id(atype, locator, step_num=step_num), seen_ids)

        step: dict = {"id": sid, "action": atype}
        if locator:
            step["locator"] = locator
        if atype in ("fill", "select") and value is not None:
            step["value"] = value
        step["settle"] = {"wait_ms": default_settle_ms}

        steps.append(step)
        loc_str = " ".join(f"{k}={v!r}" for k, v in locator.items())
        val_str = f" = {value!r}" if value else ""
        print(f"  [{atype}] {loc_str}{val_str}")

    print()
    print(f"  Session : {session_name}")
    print(f"  Start   : {start_url}")
    print(f"  Output  : {output_path}")
    print()
    print("  Interact with the site. Close the browser window when done.")
    print()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": viewport_width, "height": viewport_height},
            )
            context.add_init_script(_WEBDRIVER_HIDE)
            context.add_init_script(_RECORD_JS)

            page = context.new_page()
            page.expose_function("__trackerRecord", on_action)

            page.goto(start_url, wait_until="load")

            # Wait inside Playwright's greenlet dispatcher so expose_function
            # callbacks can be delivered. threading.Event.wait() would block the
            # dispatcher and silently drop all callbacks.
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass  # page closed or browser disconnected — expected exit

    except KeyboardInterrupt:
        print("\n  Interrupted.")
    except Exception as e:
        msg = str(e)
        if not any(s in msg for s in ("Target closed", "Browser closed", "Connection closed", "has been closed")):
            print(f"\n  Stopped: {e}")

    steps_written = len(steps)
    _write_yaml(
        {
            "session": session_name,
            "start_url": start_url,
            "viewport": {"width": viewport_width, "height": viewport_height},
            "consent": {"strategy": "ignore"},
            "steps": steps,
        },
        output_path,
    )
    print(f"\n  Wrote {steps_written} step(s) → {output_path}")
