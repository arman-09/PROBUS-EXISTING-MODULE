"""
WhatsApp notification manager for TPNODL Monitor.
Uses WhatsApp Web + Selenium (Chrome). Non-blocking init.
XPaths stored in data/wa_xpaths.json — auto-detected from live DOM.
"""
import os, time, json, logging, threading, urllib.parse
from datetime import datetime
log = logging.getLogger(__name__)

WA_PROFILE_DIR = os.path.join("data", "wa_chrome_profile")
WA_XPATH_FILE  = os.path.join("data", "wa_xpaths.json")
MAX_MSG_LEN    = 4096
SEND_TIMEOUT   = 30   # seconds to wait for message input

# ── Fallback selectors ────────────────────────────────────────────────────────
DEFAULT_XPATHS = {
    "msg_input": [
        "div[data-testid='conversation-compose-box-input']",
        "div[contenteditable='true'][data-tab='10']",
        "footer div[contenteditable='true']",
        "div[role='textbox'][data-tab='10']",
        "div[title='Type a message']",
        "div[aria-label='Type a message']",
        "div[contenteditable='true'][spellcheck='true']",
    ],
    "ready": [
        "[data-testid='chat-list']",
        "#side",
        "div[aria-label='Chat list']",
        "header[data-testid='chatlist-header']",
        "div[data-testid='conversation-list']",
    ],
    "invalid_phone": [
        "div[data-animate-modal-popup='true']",
        "div[data-testid='popup-contents']",
    ],
}

# DOM probe — ES5 only for max Chrome compatibility
DOM_PROBE_SCRIPT = """
(function() {
  var r = {msg_input:[], ready:[], invalid_phone:[], timestamp: new Date().toISOString()};
  function q(s) { try { return !!document.querySelector(s); } catch(e) { return false; } }

  var inputCandidates = [
    "div[data-testid='conversation-compose-box-input']",
    "div[contenteditable='true'][data-tab='10']",
    "footer div[contenteditable='true']",
    "div[role='textbox'][data-tab='10']",
    "div[title='Type a message']",
    "div[aria-label='Type a message']",
    "div[contenteditable='true'][spellcheck='true']",
    "div[contenteditable='true'][class*='copyable']",
    "p[class*='selectable-text'][contenteditable='true']"
  ];
  r.msg_input = inputCandidates.filter(q);

  if (r.msg_input.length === 0) {
    var els = document.querySelectorAll("div[contenteditable='true'], p[contenteditable='true']");
    var found = [];
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.closest('footer') || el.closest('#main')) {
        var tab  = el.getAttribute('data-tab');
        var tid  = el.getAttribute('data-testid');
        var aria = el.getAttribute('aria-label');
        if (tab)  found.push("div[contenteditable='true'][data-tab='" + tab + "']");
        if (tid)  found.push("[data-testid='" + tid + "']");
        if (aria) found.push("[aria-label='" + aria + "']");
      }
    }
    // deduplicate
    for (var j = 0; j < found.length; j++) {
      if (r.msg_input.indexOf(found[j]) === -1) r.msg_input.push(found[j]);
    }
    r.discovered_from_dom = r.msg_input.length > 0;
  }

  var readyCandidates = [
    "[data-testid='chat-list']", "#side",
    "div[aria-label='Chat list']", "div[data-testid='default-user']",
    "header[data-testid='chatlist-header']", "div[data-testid='conversation-list']"
  ];
  r.ready = readyCandidates.filter(q);

  var popupCandidates = [
    "div[data-animate-modal-popup='true']", "div[data-testid='popup-contents']"
  ];
  r.invalid_phone = popupCandidates.filter(q);

  return r;
})()
"""


class WhatsAppManager:
    def __init__(self, cfg):
        self.cfg     = cfg
        self._driver = None
        self._ready  = False
        self._xpaths = self._load_xpaths()
        self._lock   = threading.Lock()

    # ── Status ────────────────────────────────────────────────────────────────
    def get_status(self) -> dict:
        if self._driver is None:
            return {"ready": False, "status": "not_initialized",
                    "message": "Not connected — click Open WhatsApp Web"}
        try:
            url  = self._driver.current_url
            tabs = len(self._driver.window_handles)
            wa   = "web.whatsapp.com" in url

            # Actively check if WA is loaded (catches background thread completion)
            if not self._ready and wa:
                if self._is_wa_loaded():
                    self._ready = True
                    log.info("Status check: WA is loaded — marking ready ✓")

            if self._ready:
                return {"ready": True, "status": "ready",
                        "message": "✅ WhatsApp Web ready", "wa_open": wa, "tabs": tabs}
            elif wa:
                return {"ready": False, "status": "loading",
                        "message": "⏳ WhatsApp Web open — waiting for login/load...", "wa_open": True, "tabs": tabs}
            else:
                return {"ready": False, "status": "connected",
                        "message": "Browser open but not on WhatsApp Web", "wa_open": False, "tabs": tabs}
        except Exception:
            self._driver = None
            self._ready  = False
            return {"ready": False, "status": "not_initialized",
                    "message": "Browser disconnected — reconnect"}

    # ── XPath management ──────────────────────────────────────────────────────
    def _load_xpaths(self) -> dict:
        try:
            if os.path.exists(WA_XPATH_FILE):
                with open(WA_XPATH_FILE, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("msg_input"):
                    log.info(f"WA XPaths loaded from {WA_XPATH_FILE}")
                    return data
        except Exception as e:
            log.warning(f"WA XPaths load error: {e}")
        return dict(DEFAULT_XPATHS)

    def _save_xpaths(self, data: dict):
        try:
            data["_updated"] = datetime.now().isoformat()
            os.makedirs("data", exist_ok=True)
            with open(WA_XPATH_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log.info("WA XPaths saved")
        except Exception as e:
            log.warning(f"WA XPaths save: {e}")

    def detect_xpaths(self) -> dict:
        if not self._driver:
            return {"ok": False, "error": "Browser not connected"}
        try:
            # Switch to WA tab if not already there
            if "web.whatsapp.com" not in self._driver.current_url:
                if not self._find_wa_tab():
                    return {"ok": False, "error": "WhatsApp Web tab not found in browser"}

            result = self._driver.execute_script("return " + DOM_PROBE_SCRIPT)
            if not result:
                return {"ok": False, "error": "DOM probe returned no data — WA Web may not be fully loaded yet"}

            # result is already a dict (Selenium auto-converts JS object)
            if isinstance(result, str):
                result = json.loads(result)

            new_xpaths = {}
            for key in ("msg_input", "ready", "invalid_phone"):
                discovered = result.get(key, [])
                existing   = DEFAULT_XPATHS.get(key, [])
                merged     = list(dict.fromkeys(discovered + existing))
                new_xpaths[key] = merged

            self._xpaths = new_xpaths
            self._save_xpaths(new_xpaths)
            log.info(f"XPaths detected: {len(new_xpaths.get('msg_input',[]))} input selectors")
            return {"ok": True, "msg_input": new_xpaths["msg_input"],
                    "ready": new_xpaths["ready"],
                    "discovered_from_dom": result.get("discovered_from_dom", False),
                    "timestamp": result.get("timestamp", "")}
        except Exception as e:
            log.error(f"XPath detection error: {e}")
            return {"ok": False, "error": str(e)}

    # ── Public API ────────────────────────────────────────────────────────────
    def open_browser(self) -> dict:
        """Non-blocking: opens Chrome with WA Web. Poll status until ready."""
        if self._driver and self._ready:
            return {"ok": True, "message": "Already connected ✓"}
        if self._driver:
            try:
                _ = self._driver.title
                return {"ok": True, "message": "Browser open — waiting for login"}
            except Exception:
                self._driver = None
                self._ready  = False

        def _init_thread():
            try:
                log.info("WA: _init_thread started — scanning for existing Chrome")
                for port in (9222, 9223, 9224):
                    d = self._attach_chrome(port)
                    if d and self._find_wa_tab(d):
                        self._driver = d
                        log.info(f"WA: attached to Chrome:{port}")
                        self._wait_for_ready()
                        return
                log.info("WA: no existing Chrome found — launching new browser")
                self._launch_new_chrome()
            except Exception as e:
                log.error(f"WA _init_thread error: {e}", exc_info=True)

        threading.Thread(target=_init_thread, daemon=True, name="wa-init").start()
        return {"ok": True, "message": "Opening Chrome with WhatsApp Web..."}

    def auto_connect(self) -> dict:
        """Attach to existing Chrome or launch new one. Non-blocking."""
        if self._driver and self._ready:
            return {"ok": True, "message": "Already connected ✓"}

        def _connect():
            try:
                log.info("WA: auto_connect scanning ports 9222-9224")
                for port in (9222, 9223, 9224):
                    d = self._attach_chrome(port)
                    if d:
                        if self._find_wa_tab(d):
                            self._driver = d
                            log.info(f"WA: auto-connected to Chrome:{port} ✓")
                            # Tell Chrome to behave as if tab is always focused
                            try:
                                d.execute_cdp_cmd("Emulation.setFocusEmulationEnabled",
                                                  {"enabled": True})
                                log.info("WA: focus emulation enabled ✓")
                            except Exception as cdp_e:
                                log.debug(f"WA: CDP focus emulation: {cdp_e}")
                            self._wait_for_ready()
                            return
                        try: d.quit()
                        except Exception: pass
                # No existing Chrome with WA — launch new one
                log.info("WA: launching new Chrome for WhatsApp Web")
                self._launch_new_chrome()
            except Exception as e:
                log.error(f"WA auto_connect error: {e}", exc_info=True)

        threading.Thread(target=_connect, daemon=True, name="wa-connect").start()
        return {"ok": True, "message": "Connecting to WhatsApp Web..."}

    def close(self):
        if self._driver:
            try: self._driver.quit()
            except Exception: pass
            self._driver = None
            self._ready  = False

    # ── Send ─────────────────────────────────────────────────────────────────
    def send(self, to: list, message: str) -> bool:
        if not to: return False
        provider = self.cfg.get("whatsapp.provider", "wa_web")
        if provider == "twilio": return self._send_twilio(to, message)
        if provider == "meta":   return self._send_meta(to, message)
        return self._send_wa_web(to, message[:MAX_MSG_LEN])

    def send_test(self) -> dict:
        recipients = self.cfg.get("whatsapp.recipients") or []
        if not recipients:
            return {"ok": False, "error": "No recipients configured"}
        ok = self.send(to=recipients,
                       message="✅ TPNODL Monitor — WhatsApp test message. Notifications working.")
        return {"ok": ok, "to": recipients}

    def _send_wa_web(self, to: list, message: str) -> bool:
        # FIX: self._lock existed but was never actually acquired anywhere
        # in this class — meaning two sends close together (e.g. the
        # hourly management report's WA send and a feeder-restoration
        # alert's WA send) could both try to drive the SAME Selenium
        # browser/tab at once. A single browser tab can't safely handle
        # two concurrent "open chat / type / click send" sequences — one
        # of them ends up navigating/typing into a DOM state the other
        # just changed, throws inside _send_one(), gets caught, and
        # returns False with no distinguishing log line. That's exactly
        # what made the BILEIPADA restoration silently vanish: the alert
        # cleared correctly in the DB, but its WhatsApp message lost a
        # race against another send and was never retried.
        # Acquiring the lock here makes a second concurrent call simply
        # WAIT its turn (a few seconds' delay) instead of colliding.
        with self._lock:
            return self._send_wa_web_locked(to, message)

    def _send_wa_web_locked(self, to: list, message: str) -> bool:
        # If driver exists but not ready yet, wait up to 30s for it to become ready
        if self._driver and not self._ready:
            log.info("WA: driver initializing — waiting up to 30s...")
            for _ in range(15):
                time.sleep(2)
                if self._ready:
                    break
            if not self._ready:
                # Try active check
                try:
                    if self._is_wa_loaded():
                        self._ready = True
                except Exception:
                    pass

        if not self._ensure_ready():
            log.error("WhatsApp Web: not ready")
            return False
        results = []
        for number in to:
            results.append(self._send_one(number, message))
            if len(to) > 1:
                time.sleep(2)
        return any(results)

    def _ensure_ready(self) -> bool:
        """Ensure driver is connected and WA Web is ready to send."""
        # 1. Already have a working ready driver — just verify it's still alive
        if self._driver and self._ready:
            try:
                _ = self._driver.title  # raises if driver is dead
                return True
            except Exception:
                log.warning("WA driver died — will reconnect")
                self._driver = None
                self._ready  = False

        # 2. Driver exists but not marked ready — check DOM directly
        if self._driver:
            try:
                if self._is_wa_loaded():
                    self._ready = True
                    return True
            except Exception:
                self._driver = None
                self._ready  = False

        # 3. No driver — try attaching to existing Chrome (short timeout via socket check)
        for port in (9222, 9223, 9224):
            d = self._attach_chrome(port)
            if d and self._find_wa_tab(d):
                self._driver = d
                if self._is_wa_loaded():
                    self._ready = True
                    log.info(f"Re-attached to Chrome:{port} ✓")
                    return True
                self._wait_for_ready(timeout=10)
                if self._ready:
                    return True
                try: d.quit()
                except Exception: pass
                self._driver = None

        return False

    def _is_wa_loaded(self) -> bool:
        """Quick DOM check — is WhatsApp Web fully loaded right now?"""
        from selenium.webdriver.common.by import By
        try:
            url = self._driver.current_url
            if "web.whatsapp.com" not in url:
                if not self._find_wa_tab():
                    return False
            ready_sels = self._xpaths.get("ready", DEFAULT_XPATHS["ready"])
            for sel in ready_sels:
                try:
                    if self._driver.find_element(By.CSS_SELECTOR, sel):
                        return True
                except Exception:
                    continue
            return False
        except Exception:
            return False

    def _send_one(self, number: str, message: str) -> bool:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        num = "".join(c for c in number if c.isdigit())
        if not num:
            log.warning(f"WhatsApp: invalid number '{number}'")
            return False

        try:
            # Ensure we are on WA Web
            if "web.whatsapp.com" not in self._driver.current_url:
                self._find_wa_tab()

            # Open chat via send URL
            url = f"https://web.whatsapp.com/send?phone={num}&text=&source=&data="
            self._driver.get(url)

            # Un-throttle background tab JS before waiting
            self._driver.execute_script("""
                if (document.hidden !== undefined) {
                    Object.defineProperty(document, 'hidden', {value: false, writable: true});
                    Object.defineProperty(document, 'visibilityState', {value: 'visible', writable: true});
                    document.dispatchEvent(new Event('visibilitychange'));
                }
            """)
            time.sleep(2)

            # Check for invalid phone popup
            for sel in self._xpaths.get("invalid_phone", []):
                try:
                    el = self._driver.find_element(By.CSS_SELECTOR, sel)
                    if el and el.is_displayed():
                        txt = el.text.lower()
                        if any(w in txt for w in ("invalid", "not exist", "can't message", "phone number")):
                            log.error(f"Invalid phone number: +{num}")
                            return False
                except Exception:
                    pass

            # Find input box
            selectors   = self._xpaths.get("msg_input", DEFAULT_XPATHS["msg_input"])
            input_el    = None
            working_sel = None
            for sel in selectors:
                try:
                    input_el = WebDriverWait(self._driver, SEND_TIMEOUT).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    working_sel = sel
                    break
                except Exception:
                    continue

            if not input_el:
                probe = self.detect_xpaths()
                if probe.get("ok"):
                    for sel in probe.get("msg_input", []):
                        try:
                            input_el = WebDriverWait(self._driver, 10).until(
                                EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                            working_sel = sel
                            break
                        except Exception:
                            continue

            if not input_el:
                log.error(f"Message input not found for +{num}")
                self._driver.get("https://web.whatsapp.com")
                time.sleep(2)
                return False

            if working_sel and working_sel in selectors and selectors[0] != working_sel:
                selectors.remove(working_sel)
                selectors.insert(0, working_sel)
                self._xpaths["msg_input"] = selectors
                self._save_xpaths(self._xpaths)

            time.sleep(0.3)

            # ── Send via JavaScript — works when minimized ───────────────────
            # Strategy: inject text via React's synthetic event system
            # This bypasses the need for window focus or visible rendering
            sent = self._js_send(input_el, message)

            if not sent:
                # Fallback: pyperclip clipboard paste
                try:
                    import pyperclip
                    pyperclip.copy(message)
                    self._driver.execute_script("window.focus(); arguments[0].focus();", input_el)
                    time.sleep(0.3)
                    from selenium.webdriver.common.keys import Keys
                    input_el.send_keys(Keys.CONTROL + 'v')
                    time.sleep(0.8)
                    sent = bool(self._driver.execute_script(
                        "return (arguments[0].innerText||'').trim()", input_el))
                    if sent:
                        log.debug("WA: pyperclip paste OK")
                except Exception as e:
                    log.debug(f"WA: pyperclip failed ({e})")

            if not sent:
                log.error(f"WA: all send methods failed for +{num}")
                return False

            # Press Enter via JS key event (no send_keys needed)
            self._driver.execute_script("""
                arguments[0].dispatchEvent(new KeyboardEvent('keydown',
                    {bubbles:true,cancelable:true,keyCode:13,which:13,key:'Enter'}));
                arguments[0].dispatchEvent(new KeyboardEvent('keyup',
                    {bubbles:true,cancelable:true,keyCode:13,which:13,key:'Enter'}));
            """, input_el)
            time.sleep(2)
            log.info(f"WhatsApp Web: sent to +{num} ✓")
            return True

        except Exception as e:
            log.error(f"WhatsApp send to +{num}: {e}")
            try:
                self._driver.get("https://web.whatsapp.com")
                time.sleep(2)
            except Exception:
                pass
            return False

    def _js_send(self, input_el, message: str) -> bool:
        """
        Inject message text via JavaScript — designed to work when Chrome is minimized.

        Strategy order:
        1. WA Web Store API  — direct React state injection (no UI needed at all)
        2. ClipboardEvent    — DataTransfer paste into React contenteditable
        3. nativeInputSetter — Override innerText via property descriptor
        4. execCommand       — document.execCommand insertText (legacy fallback)
        """

        # ── Force un-throttle background tab before any JS injection ──────────
        try:
            self._driver.execute_script("""
                try {
                    Object.defineProperty(document, 'hidden',
                        {value:false, configurable:true, writable:true});
                    Object.defineProperty(document, 'visibilityState',
                        {value:'visible', configurable:true, writable:true});
                    document.dispatchEvent(new Event('visibilitychange'));
                    window.dispatchEvent(new Event('focus'));
                } catch(e) {}
            """)
        except Exception:
            pass

        # ── Strategy 1: WA Web Store API (works 100% regardless of window state) ──
        # WA Web exposes window.Store after initial load — inject directly into React
        try:
            result = self._driver.execute_script("""
                var msg = arguments[0];
                try {
                    // WA Web 2024+ Store injection via module
                    var store = window.Store;
                    if (!store) {
                        // Try to find Store via require()
                        var req = window.require || window.webpackChunkwhatsapp_web_client;
                        if (req && typeof req === 'function') {
                            req(['WAWebCollectionsStore'], function(s){ store = s; });
                        }
                    }
                    if (!store || !store.Chat) return 'no_store';

                    var chat = store.Chat.getModels().find(function(c) {
                        return c.active;
                    });
                    if (!chat) return 'no_active_chat';

                    // Inject into compose box
                    var compose = store.MsgKey || window.Store.Cmd;
                    if (store.Cmd && store.Cmd.sendTextMsgToChat) {
                        store.Cmd.sendTextMsgToChat(chat, msg);
                        return 'store_sent';
                    }
                    return 'store_no_cmd';
                } catch(e) { return 'err:' + e.message; }
            """, message)
            if result == 'store_sent':
                log.debug("WA: Strategy 1 (Store API) OK")
                time.sleep(1)
                return True
            else:
                log.debug(f"WA: Strategy 1 skipped ({result})")
        except Exception as e:
            log.debug(f"WA: Strategy 1 failed: {e}")

        # ── Strategy 2: ClipboardEvent (DataTransfer paste) ───────────────────
        try:
            self._driver.execute_script("""
                var el  = arguments[0];
                var msg = arguments[1];
                el.focus();
                var dt = new DataTransfer();
                dt.setData('text/plain', msg);
                el.dispatchEvent(new ClipboardEvent('paste',
                    {bubbles:true, cancelable:true, clipboardData:dt}));
            """, input_el, message)
            time.sleep(1.0)
            content = self._driver.execute_script(
                "return (arguments[0].innerText||arguments[0].textContent||'').trim()", input_el)
            if content:
                log.debug("WA: Strategy 2 (ClipboardEvent) OK")
                return True
        except Exception as e:
            log.debug(f"WA: Strategy 2 failed: {e}")

        # ── Strategy 3: nativeInputValueSetter ────────────────────────────────
        try:
            self._driver.execute_script("""
                var el  = arguments[0];
                var msg = arguments[1];
                el.focus();
                // Use innerText property descriptor to bypass React controlled state
                var desc = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'innerText');
                if (desc && desc.set) desc.set.call(el, msg);
                el.dispatchEvent(new InputEvent('input',
                    {bubbles:true, cancelable:true, data:msg, inputType:'insertText'}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, input_el, message)
            time.sleep(1.0)
            content = self._driver.execute_script(
                "return (arguments[0].innerText||'').trim()", input_el)
            if content:
                log.debug("WA: Strategy 3 (nativeInputSetter) OK")
                return True
        except Exception as e:
            log.debug(f"WA: Strategy 3 failed: {e}")

        # ── Strategy 4: execCommand insertText ────────────────────────────────
        try:
            self._driver.execute_script("""
                var el  = arguments[0];
                var msg = arguments[1];
                el.focus();
                document.execCommand('selectAll', false, null);
                document.execCommand('insertText', false, msg);
            """, input_el, message)
            time.sleep(1.0)
            content = self._driver.execute_script(
                "return (arguments[0].innerText||'').trim()", input_el)
            if content:
                log.debug("WA: Strategy 4 (execCommand) OK")
                return True
        except Exception as e:
            log.debug(f"WA: Strategy 4 failed: {e}")

        log.warning("WA: all JS strategies failed — input box may not have rendered")
        return False

    # ── Chrome helpers ────────────────────────────────────────────────────────
    def _attach_chrome(self, port: int):
        """Attach to Chrome already running with --remote-debugging-port."""
        try:
            import socket as _sock
            s = _sock.create_connection(("127.0.0.1", port), timeout=1)
            s.close()
            log.info(f"Port {port} is open — attempting Chrome attach")
        except Exception:
            return None

        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service
            import glob as _glob, shutil

            opts = Options()
            opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")
            # Disable background tab throttling on existing Chrome via CDP args
            opts.add_argument("--disable-background-timer-throttling")
            opts.add_argument("--disable-backgrounding-occluded-windows")
            opts.add_argument("--disable-renderer-backgrounding")

            home = os.path.expanduser("~")
            candidates = []
            cfg_path = self.cfg.get("whatsapp.chromedriver_path", "")
            if cfg_path and os.path.exists(cfg_path):
                candidates.append(cfg_path)
            sys_cd = shutil.which("chromedriver")
            if sys_cd: candidates.append(sys_cd)
            candidates += sorted(_glob.glob(os.path.join(
                home, ".cache", "selenium", "chromedriver", "win64", "*", "chromedriver.exe")), reverse=True)
            candidates += sorted(_glob.glob(os.path.join(
                home, ".wdm", "drivers", "chromedriver", "win64", "*", "chromedriver-win64", "chromedriver.exe")), reverse=True)

            for path in candidates:
                try:
                    d = webdriver.Chrome(service=Service(executable_path=path), options=opts)
                    _ = d.title
                    log.info(f"Connected to Chrome:{port} via {path}")
                    # Disable throttling via CDP on the attached session
                    try:
                        d.execute_cdp_cmd("Emulation.setFocusEmulationEnabled", {"enabled": True})
                    except Exception:
                        pass
                    return d
                except Exception:
                    continue

            try:
                d = webdriver.Chrome(options=opts)
                _ = d.title
                log.info(f"Connected to Chrome:{port}")
                return d
            except Exception:
                pass

        except Exception as e:
            log.debug(f"Attach Chrome:{port}: {e}")
        return None

    def _launch_new_chrome(self) -> bool:
        """Open Chrome with saved WA profile and remote debugging."""
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.options import Options
            from selenium.webdriver.chrome.service import Service

            os.makedirs(WA_PROFILE_DIR, exist_ok=True)
            opts = Options()
            opts.add_argument(f"--user-data-dir={os.path.abspath(WA_PROFILE_DIR)}")
            opts.add_argument("--profile-directory=Default")
            opts.add_argument("--no-sandbox")
            opts.add_argument("--disable-dev-shm-usage")
            opts.add_argument("--disable-blink-features=AutomationControlled")
            opts.add_argument("--start-maximized")
            opts.add_argument("--disable-extensions")
            opts.add_argument("--remote-debugging-port=9222")
            opts.add_argument("--disable-background-timer-throttling")
            opts.add_argument("--disable-backgrounding-occluded-windows")
            opts.add_argument("--disable-renderer-backgrounding")
            opts.add_experimental_option("excludeSwitches", ["enable-automation"])
            opts.add_experimental_option("useAutomationExtension", False)

            home = os.path.expanduser("~")
            candidate_paths = []

            # 0. Explicitly configured path (highest priority)
            cfg_path = self.cfg.get("whatsapp.chromedriver_path", "")
            if cfg_path and os.path.exists(cfg_path):
                candidate_paths.append(cfg_path)

            # 1. System PATH
            import shutil
            sys_path = shutil.which("chromedriver")
            if sys_path:
                candidate_paths.insert(0, sys_path)

            # 2. Selenium 4.6+ cache
            import glob as _glob
            for p in _glob.glob(os.path.join(home, ".cache", "selenium", "chromedriver", "win64", "*", "chromedriver.exe")):
                candidate_paths.append(p)
            # 3. WDM cache
            for p in _glob.glob(os.path.join(home, ".wdm", "drivers", "chromedriver", "win64", "*", "chromedriver-win64", "chromedriver.exe")):
                candidate_paths.append(p)

            # Sort newest version first
            candidate_paths = sorted(set(candidate_paths), reverse=True)
            log.info(f"ChromeDriver candidates: {candidate_paths}")

            self._driver = None
            for path in candidate_paths:
                try:
                    log.info(f"Trying ChromeDriver: {path}")
                    svc = Service(executable_path=path)
                    self._driver = webdriver.Chrome(service=svc, options=opts)
                    log.info(f"Chrome started with: {path}")
                    break
                except Exception as e:
                    log.warning(f"Failed with {path}: {e}")
                    continue

            # Last resort: let Selenium find it automatically
            if not self._driver:
                try:
                    log.info("Trying Selenium built-in driver manager...")
                    self._driver = webdriver.Chrome(options=opts)
                    log.info("Chrome started via Selenium built-in manager")
                except Exception as e:
                    log.error(f"All ChromeDriver attempts failed: {e}")
                    return False

            self._driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"})
            self._driver.implicitly_wait(5)
            self._driver.get("https://web.whatsapp.com")
            log.info("WhatsApp Web opened — waiting for login...")
            self._wait_for_ready()
            return self._ready

        except Exception as e:
            log.error(f"Chrome launch error: {e}", exc_info=True)
            self._driver = None
            return False

    def _find_wa_tab(self, driver=None) -> bool:
        """Switch to WA Web tab. Uses self._driver if driver not given."""
        d = driver or self._driver
        if not d: return False
        try:
            for handle in d.window_handles:
                d.switch_to.window(handle)
                if "web.whatsapp.com" in d.current_url:
                    log.info(f"Found WhatsApp Web tab ✓")
                    return True
        except Exception as e:
            log.warning(f"Tab search: {e}")
        return False

    def _wait_for_ready(self, timeout: int = 120):
        """Wait for WA Web to fully load. Sets self._ready = True when done."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        import time as _time

        ready_sels = self._xpaths.get("ready", DEFAULT_XPATHS["ready"])
        deadline   = _time.time() + timeout
        per_sel    = min(10, timeout // max(len(ready_sels), 1))  # max 10s per selector

        while _time.time() < deadline:
            for sel in ready_sels:
                try:
                    remaining = int(deadline - _time.time())
                    wait_t    = min(per_sel, max(1, remaining))
                    WebDriverWait(self._driver, wait_t).until(
                        EC.presence_of_element_located((By.CSS_SELECTOR, sel)))
                    self._ready = True
                    log.info(f"WhatsApp Web: logged in ✓ (matched: {sel})")
                    self.detect_xpaths()
                    return
                except Exception:
                    continue
            _time.sleep(2)

        log.warning("WhatsApp Web: login wait timed out")
        self._ready = False

    def get_walink(self, number: str, message: str) -> str:
        num = "".join(c for c in number if c.isdigit())
        return f"https://wa.me/{num}?text={urllib.parse.quote(message)}"

    # ── Twilio ────────────────────────────────────────────────────────────────
    def _send_twilio(self, to: list, message: str) -> bool:
        try:
            from twilio.rest import Client
            client = Client(self.cfg.get("whatsapp.twilio_sid",""),
                            self.cfg.get("whatsapp.twilio_token",""))
            from_ = self.cfg.get("whatsapp.twilio_from","whatsapp:+14155238886")
            for num in to:
                n = "".join(c for c in num if c.isdigit())
                client.messages.create(body=message, from_=from_, to=f"whatsapp:+{n}")
            return True
        except Exception as e:
            log.error(f"Twilio WA error: {e}")
            return False

    # ── Meta Cloud API ────────────────────────────────────────────────────────
    def _send_meta(self, to: list, message: str) -> bool:
        try:
            import requests
            token    = self.cfg.get("whatsapp.meta_token","")
            phone_id = self.cfg.get("whatsapp.meta_phone_id","")
            for num in to:
                n = "".join(c for c in num if c.isdigit())
                requests.post(
                    f"https://graph.facebook.com/v19.0/{phone_id}/messages",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"messaging_product":"whatsapp","to":n,
                          "type":"text","text":{"body":message[:MAX_MSG_LEN]}},
                    timeout=15)
            return True
        except Exception as e:
            log.error(f"Meta WA error: {e}")
            return False
