import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from itertools import count
from typing import Any

from websockets.sync.client import connect as websocket_connect


MCP_STATE = Path(tempfile.gettempdir()) / "colab-mcp-current.json"
DEFAULT_CDP_PORT = 9333
DEFAULT_PROFILE = Path.home() / ".codex" / "edge-colab-mcp-profile"
COLAB_URL_FRAGMENT = "colab.research.google.com"
LOGIN_REQUIRED_MESSAGE = (
    "Colab is not logged in in the dedicated MCP browser. Ask the user to log "
    "into Google/Colab in the Edge window opened by colabctl, then rerun the command."
)


VISIBLE_INTERACTIVES_JS = r"""
(() => Array.from(document.querySelectorAll('button,a,[role=button],mwc-button,paper-button,.goog-menuitem,[aria-label]'))
  .filter(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length))
  .slice(0, 300)
  .map((e, i) => ({
    i,
    tag: e.tagName,
    text: (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim().slice(0, 200),
    aria: e.getAttribute('aria-label'),
    title: e.getAttribute('title'),
    href: e.href || '',
  })))()
"""


def get_json(url: str, timeout: float = 2) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def edge_path() -> Path:
    candidates = [
        os.environ.get("ProgramFiles(x86)", "")
        + r"\Microsoft\Edge\Application\msedge.exe",
        os.environ.get("ProgramFiles", "") + r"\Microsoft\Edge\Application\msedge.exe",
    ]
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    raise RuntimeError("Microsoft Edge executable was not found.")


def cdp_alive(port: int) -> bool:
    try:
        get_json(f"http://127.0.0.1:{port}/json/version", timeout=1)
        return True
    except Exception:
        return False


def start_edge(port: int, profile: Path, url: str) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    if cdp_alive(port):
        return
    subprocess.Popen(
        [
            str(edge_path()),
            "--remote-debugging-address=127.0.0.1",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--new-window",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if cdp_alive(port):
            return
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for Edge CDP on port {port}.")


def pages(port: int) -> list[dict[str, Any]]:
    tabs = get_json(f"http://127.0.0.1:{port}/json/list")
    return [tab for tab in tabs if tab.get("type") == "page"]


def colab_mcp_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*colab-mcp.exe*' -or $_.CommandLine -match 'python(\\.exe)?\"?\\s+-m\\s+colab_mcp(\\s|$)' } | "
        "Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            text=True,
            capture_output=True,
            timeout=5,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            return []
        data = json.loads(completed.stdout)
        if isinstance(data, dict):
            data = [data]
        return data
    except Exception:
        return []


def select_colab_tab(port: int) -> dict[str, Any]:
    for tab in pages(port):
        if COLAB_URL_FRAGMENT in tab.get("url", ""):
            return tab
    raise RuntimeError("No Colab tab found. Run `colabctl connect` first.")


def create_or_get_colab_tab(port: int, url: str) -> dict[str, Any]:
    existing = pages(port)
    for tab in existing:
        if COLAB_URL_FRAGMENT in tab.get("url", ""):
            return tab
    encoded = urllib.parse.quote(url, safe="")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/json/new?{encoded}", method="PUT"
    )
    with urllib.request.urlopen(request, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


class CdpSession:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self._ids = count(1)
        self._ws = None

    def __enter__(self):
        self._ws = websocket_connect(self.websocket_url, open_timeout=2)
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._ws is not None:
            self._ws.close()
        self._ws = None

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._ws is None:
            raise RuntimeError("CDP session is not open.")
        message_id = next(self._ids)
        self._ws.send(
            json.dumps({"id": message_id, "method": method, "params": params or {}})
        )
        while True:
            message = json.loads(self._ws.recv())
            if message.get("id") == message_id:
                if "error" in message:
                    raise RuntimeError(json.dumps(message["error"], ensure_ascii=False))
                return message.get("result")

    def evaluate(self, expression: str, await_promise: bool = False) -> Any:
        return self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )


def eval_colab(port: int, expression: str, await_promise: bool = False) -> Any:
    tab = select_colab_tab(port)
    with CdpSession(tab["webSocketDebuggerUrl"]) as cdp:
        return cdp.evaluate(expression, await_promise=await_promise)


def active_state() -> dict[str, Any]:
    try:
        return json.loads(MCP_STATE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def command_status(args: argparse.Namespace) -> None:
    state = active_state()
    browser = None
    if cdp_alive(args.port):
        try:
            tab = select_colab_tab(args.port)
            value = eval_colab(
                args.port,
                """
JSON.stringify((()=>{
  const params = new URL(location.href).hash.replace(/^#/, '').split('&').reduce((o, part) => {
    const [k, v] = part.split('=');
    if (k) o[decodeURIComponent(k)] = decodeURIComponent(v || '');
    return o;
  }, {});
  const sessionToken = sessionStorage.getItem('mcp_proxy_token');
  const sessionPort = sessionStorage.getItem('mcp_proxy_port');
  const notebook = window.colab?.global?.notebook;
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  const loginRequired = !email || email === 'anonymous' || /Sign in|\u767b\u5f55|\u767b\u5165/.test(body);
  const localMcp = !!notebook?.localColabMcpService;
  const connected = !!notebook?.localColabMcpService?.isConnected?.();
  const runtimeConnectedHint = Boolean(notebook?.kernel || notebook?.kernelLastConnectedTimeMs);
  const gpuRuntimeHint = /\b(T4|L4|A100|V100|GPU|TPU)\b/i.test(body);
  const warnings = [];
  if ((params.mcpProxyToken && sessionToken && params.mcpProxyToken !== sessionToken) || (params.mcpProxyPort && sessionPort && params.mcpProxyPort !== sessionPort)) {
    warnings.push('hash/sessionStorage MCP token or port mismatch');
  }
  if (localMcp && !connected) warnings.push('Colab page has MCP service but browser is not connected to local MCP');
  if (!runtimeConnectedHint) warnings.push('Colab runtime connection is not detected');
  if (!gpuRuntimeHint) warnings.push('No GPU/TPU accelerator hint detected in the Colab UI');
  if (loginRequired) warnings.push('Colab login is required in the dedicated MCP browser');
  return {
    href: location.href,
    ready: document.readyState,
    email,
    loginRequired,
    loginMessage: loginRequired
      ? 'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'
      : null,
    token: sessionToken,
    port: sessionPort,
    hashToken: params.mcpProxyToken || null,
    hashPort: params.mcpProxyPort || null,
    hashSessionMismatch: warnings.includes('hash/sessionStorage MCP token or port mismatch'),
    localMcp,
    connected,
    staleLocalMcp: localMcp && !connected,
    runtimeConnectedHint,
    kernelLastConnectedTimeMs: notebook?.kernelLastConnectedTimeMs || null,
    gpuRuntimeHint,
    warnings,
  };
})())
""",
            )["result"]["value"]
            browser = json.loads(value)
            browser["tabId"] = tab.get("id")
        except Exception as exc:
            browser = {"error": str(exc)}
    print(
        json.dumps(
            {
                "mcpState": state,
                "edgeCdpPort": args.port,
                "browser": browser,
                "loginRequired": bool(browser and browser.get("loginRequired")),
                "promptForAi": LOGIN_REQUIRED_MESSAGE
                if browser and browser.get("loginRequired")
                else None,
                "colabMcpProcesses": colab_mcp_processes(),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def command_connect(args: argparse.Namespace) -> None:
    state = active_state()
    if not state:
        raise RuntimeError(
            f"No MCP state file found at {MCP_STATE}. Start colab-mcp first."
        )
    processes = colab_mcp_processes()
    state_pid = state.get("pid")
    other_processes = [
        proc
        for proc in processes
        if int(proc.get("ProcessId", -1)) != int(state_pid or -1)
        and int(proc.get("ProcessId", -1)) != int(os.getpid())
        and int(proc.get("ProcessId", -1)) != int(os.getppid())
        and int(proc.get("ParentProcessId", -1)) != int(state_pid or -1)
        and int(proc.get("ProcessId", -1))
        not in {
            int(p.get("ParentProcessId", -1))
            for p in processes
            if int(p.get("ProcessId", -1)) == int(state_pid or -1)
        }
    ]
    if other_processes and not args.allow_ambiguous:
        raise RuntimeError(
            "Multiple colab-mcp-like processes are running. Re-run with "
            "`--allow-ambiguous` or stop stale servers. "
            f"activeStatePid={state_pid}, otherPids={[p.get('ProcessId') for p in other_processes]}"
        )
    scratch_url = state["scratchUrl"]
    if getattr(args, "url", None):
        target_base = str(args.url).strip().split("#")[0]
        if target_base.startswith("/"):
            target_base = f"https://colab.research.google.com{target_base}"
        scratch_url = (
            f"{target_base}#mcpProxyToken={state['token']}&mcpProxyPort={state['port']}"
        )
    start_edge(args.port, args.profile, scratch_url)
    tab = create_or_get_colab_tab(args.port, scratch_url)
    with CdpSession(tab["webSocketDebuggerUrl"]) as cdp:
        cdp.call("Page.navigate", {"url": scratch_url})
    deadline = time.monotonic() + args.timeout
    last = None
    while time.monotonic() < deadline:
        try:
            value = eval_colab(
                args.port,
                "JSON.stringify((()=>{const body=document.body?.innerText||'';const email=window.colabUserEmail||null;const loginRequired=!email||email==='anonymous'||/Sign in|\\u767b\\u5f55|\\u767b\\u5165/.test(body);return {ready: document.readyState, email, loginRequired, hasService: !!window.colab?.global?.notebook?.localColabMcpService, token: sessionStorage.getItem('mcp_proxy_token'), port: sessionStorage.getItem('mcp_proxy_port')}})())",
            )["result"]["value"]
            last = json.loads(value)
            if last.get("ready") == "complete" and last.get("hasService"):
                break
        except Exception as exc:
            last = {"error": str(exc)}
        time.sleep(1)
    if last and last.get("loginRequired"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "loginRequired": True,
                    "email": last.get("email"),
                    "message": LOGIN_REQUIRED_MESSAGE,
                    "promptForAi": LOGIN_REQUIRED_MESSAGE,
                    "profile": str(args.profile),
                    "edgeCdpPort": args.port,
                },
                ensure_ascii=False,
            )
        )
        return
    token = json.dumps(state["token"])
    mcp_port = json.dumps(str(state["port"]))
    connect_js = (
        "(async()=>{"
        "const svc=window.colab?.global?.notebook?.localColabMcpService;"
        "if(!svc) return JSON.stringify({ok:false,error:'localColabMcpService missing'});"
        f"const targetToken={token};"
        f"const targetPort={mcp_port};"
        "const before={connected:svc.isConnected?.(),token:sessionStorage.getItem('mcp_proxy_token'),port:sessionStorage.getItem('mcp_proxy_port'),href:location.href};"
        "const mismatch=before.token!==targetToken||before.port!==targetPort||!location.hash.includes(targetToken)||!location.hash.includes(targetPort);"
        "if(mismatch&&svc.disconnect){try{await svc.disconnect();}catch(e){}}"
        "sessionStorage.setItem('mcp_proxy_token',targetToken);"
        "sessionStorage.setItem('mcp_proxy_port',targetPort);"
        "const base=location.href.split('#')[0];"
        "history.replaceState(null,'',`${base}#mcpProxyToken=${encodeURIComponent(targetToken)}&mcpProxyPort=${encodeURIComponent(targetPort)}`);"
        "let alreadyConnected=false;"
        "try{await Promise.race([svc.connect(),new Promise((_,reject)=>setTimeout(()=>reject(new Error('localColabMcpService.connect timed out')),20000))]);}"
        "catch(e){"
        "const msg=String(e?.message||e||'');"
        "if(msg.includes('MCP server already connected')&&svc.isConnected?.()){alreadyConnected=true;}"
        "else{throw e;}"
        "}"
        "return JSON.stringify({ok:true, before, mismatch, alreadyConnected, connected:svc.isConnected(), token:sessionStorage.getItem('mcp_proxy_token'), port:sessionStorage.getItem('mcp_proxy_port'), href:location.href});"
        "})()"
    )
    evaluation = eval_colab(args.port, connect_js, await_promise=True)
    if "exceptionDetails" in evaluation:
        raise RuntimeError(
            "Colab connect JavaScript failed: "
            + json.dumps(evaluation["exceptionDetails"], ensure_ascii=False)
        )
    value = evaluation.get("result", {}).get("value")
    if not isinstance(value, str):
        verify = eval_colab(
            args.port,
            "JSON.stringify({connected:!!window.colab?.global?.notebook?.localColabMcpService?.isConnected?.(),token:sessionStorage.getItem('mcp_proxy_token'),port:sessionStorage.getItem('mcp_proxy_port'),href:location.href})",
        )
        raise RuntimeError(
            "Colab connect JavaScript returned a non-string result. "
            + json.dumps(
                {"evaluation": evaluation, "verify": verify},
                ensure_ascii=False,
            )
        )
    print(value)


def command_eval(args: argparse.Namespace) -> None:
    expr = args.expression
    if args.file:
        expr = Path(args.file).read_text(encoding="utf-8")
    print(
        json.dumps(
            eval_colab(args.port, expr, args.await_promise),
            ensure_ascii=False,
            indent=2,
        )
    )


def command_list_tabs(args: argparse.Namespace) -> None:
    print(json.dumps({"tabs": pages(args.port)}, ensure_ascii=False, indent=2))


def command_snapshot(args: argparse.Namespace) -> None:
    print(
        json.dumps(
            eval_colab(args.port, VISIBLE_INTERACTIVES_JS), ensure_ascii=False, indent=2
        )
    )


def command_click_text(args: argparse.Namespace) -> None:
    needle = json.dumps(args.text, ensure_ascii=False)
    expression = rf"""
(() => {{
  const needle = {needle};
  const candidates = Array.from(document.querySelectorAll('button,a,[role=button],mwc-button,paper-button,.goog-menuitem,[aria-label]'));
  const target = candidates.find(e => {{
    const label = (e.innerText || e.textContent || e.getAttribute('aria-label') || '').trim();
    return label.includes(needle) && !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  }});
  if (!target) return {{clicked: false, reason: 'not found', text: needle}};
  target.click();
  return {{clicked: true, tag: target.tagName, text: (target.innerText || target.textContent || target.getAttribute('aria-label') || '').trim()}};
}})()
"""
    print(json.dumps(eval_colab(args.port, expression), ensure_ascii=False, indent=2))


def command_keepalive(args: argparse.Namespace) -> None:
    script = r"""
(() => {
  const body = document.body;
  if (!body) return JSON.stringify({ok:false,error:'document.body missing'});
  window.focus();
  const target =
    document.querySelector('colab-run-button') ||
    document.querySelector('[role="main"]') ||
    document.querySelector('.notebook') ||
    body;
  const rect = target.getBoundingClientRect?.() || body.getBoundingClientRect();
  const x = Math.max(8, Math.min(window.innerWidth - 8, rect.left + Math.min(rect.width || window.innerWidth, 180) / 2));
  const y = Math.max(8, Math.min(window.innerHeight - 8, rect.top + Math.min(rect.height || window.innerHeight, 120) / 2));
  const events = ['pointerover', 'pointermove', 'mousemove', 'mousedown', 'mouseup', 'click'];
  for (let i = 0; i < 2; i++) {
    for (const type of events) {
      target.dispatchEvent(new MouseEvent(type, {bubbles:true, cancelable:true, view:window, clientX:x + i, clientY:y + i}));
    }
  }
  document.dispatchEvent(new KeyboardEvent('keydown', {key:'Shift', bubbles:true}));
  document.dispatchEvent(new KeyboardEvent('keyup', {key:'Shift', bubbles:true}));
  return JSON.stringify({
    ok: true,
    href: location.href,
    ready: document.readyState,
    timestamp: Date.now(),
    targetTag: target.tagName,
    x,
    y,
    connected: !!window.colab?.global?.notebook?.localColabMcpService?.isConnected?.(),
  });
})()
"""
    tab = select_colab_tab(args.port)
    beats = 0
    outputs = []
    while True:
        beats += 1
        with CdpSession(tab["webSocketDebuggerUrl"]) as cdp:
            with contextlib.suppress(Exception):
                cdp.call("Page.bringToFront")
            result = cdp.evaluate(script)
        value = result.get("result", {}).get("value")
        try:
            payload = (
                json.loads(value)
                if isinstance(value, str)
                else {"ok": False, "raw": result}
            )
        except json.JSONDecodeError:
            payload = {"ok": False, "raw": value}
        payload["beat"] = beats
        outputs.append(payload)
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        if not args.forever and beats >= args.count:
            break
        time.sleep(args.interval)


def accelerator_script(accelerator: str, apply: bool) -> str:
    accelerator_json = json.dumps(accelerator, ensure_ascii=False)
    apply_json = "true" if apply else "false"
    return rf"""
(async () => {{
  const accelerator = {accelerator_json};
  const shouldApply = {apply_json};
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const runtimeMenuLabels = ['Runtime', 'Code execution program', '\u4ee3\u7801\u6267\u884c\u7a0b\u5e8f'];
  const runtimeDialogLabels = ['Change runtime type', 'Runtime type', '\u66f4\u6539\u8fd0\u884c\u65f6\u7c7b\u578b', '\u8fd0\u884c\u65f6\u7c7b\u578b'];
  const acceleratorLabels = ['Hardware accelerator', 'Accelerator', '\u786c\u4ef6\u52a0\u901f\u5668'];
  const applyLabels = ['Save', 'Apply', 'OK', 'Yes', 'Restart runtime', '\u4fdd\u5b58', '\u786e\u5b9a', '\u662f'];
  const targetLabels = accelerator.toLowerCase() === 'gpu'
    ? ['T4 GPU', 'L4 GPU', 'A100 GPU', 'H100 GPU', 'G4 GPU', 'GPU']
    : accelerator.toLowerCase() === 'none'
    ? ['None', 'No accelerator', 'CPU']
    : [accelerator, accelerator.toUpperCase(), accelerator.toLowerCase()];
  const out = {{ ok: false, accelerator, applied: false, steps: [], warnings: [] }};

  function allDeep(root = document) {{
    const nodes = [];
    const walk = node => {{
      if (!node) return;
      if (node.nodeType === Node.ELEMENT_NODE) {{
        nodes.push(node);
        if (node.shadowRoot) walk(node.shadowRoot);
      }}
      for (const child of node.children || []) walk(child);
    }};
    walk(root);
    return nodes;
  }}

  function visible(el) {{
    const rect = el.getBoundingClientRect?.();
    return Boolean(rect && rect.width > 0 && rect.height > 0);
  }}

  function label(el) {{
    return (
      el.getAttribute?.('aria-label') ||
      el.getAttribute?.('title') ||
      el.innerText ||
      el.textContent ||
      ''
    ).trim();
  }}

  function interactive(el) {{
    const tag = el.tagName || '';
    const role = el.getAttribute?.('role') || '';
    return tag === 'BUTTON' || tag === 'A' || tag.startsWith('MD-') || tag.startsWith('MWC-') ||
      tag.startsWith('PAPER-') || tag === 'COLAB-TOOLBAR-BUTTON' ||
      ['button', 'menuitem', 'option', 'link', 'combobox', 'listbox'].includes(role);
  }}

  function clickElement(el) {{
    el.scrollIntoView?.({{ block: 'center', inline: 'center' }});
    el.dispatchEvent(new MouseEvent('mouseover', {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent('mousedown', {{ bubbles: true, cancelable: true, view: window }}));
    el.dispatchEvent(new MouseEvent('mouseup', {{ bubbles: true, cancelable: true, view: window }}));
    el.click?.();
    return {{ clicked: true, tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 200) }};
  }}

  function findByLabels(labels, exact = false) {{
    const candidates = allDeep().filter(el => visible(el));
    for (const needleRaw of labels) {{
      const needle = String(needleRaw).toLowerCase();
      const ranked = candidates
        .map(el => ({{
          el,
          text: label(el).toLowerCase(),
          score: (el.tagName === 'INPUT' ? 4 : 0) + (el.tagName === 'LABEL' ? 3 : 0) + (interactive(el) ? 2 : 0),
        }}))
        .filter(item => item.text && (exact ? item.text === needle : item.text.includes(needle)))
        .sort((a, b) => b.score - a.score);
      if (ranked[0]?.el) return ranked[0].el;
    }}
    return null;
  }}

  function clickByLabels(labels, exact = false) {{
    const direct = findByLabels(labels, exact);
    if (!direct) return {{ clicked: false, reason: 'not found', labels }};
    if (interactive(direct)) return clickElement(direct);
    const ancestor = direct.closest?.('button,[role=button],[role=menuitem],[role=option],[role=combobox],mwc-button,paper-button,md-outlined-select,md-select') || direct;
    return clickElement(ancestor);
  }}

  function closeExistingDialogs() {{
    const dialogs = allDeep().filter(el => visible(el) && ['dialog', 'alertdialog'].includes(el.getAttribute?.('role') || ''));
    for (const dialog of dialogs) {{
      const buttons = allDeep(dialog).filter(el => visible(el));
      const target = buttons.find(el => ['Cancel', 'Close', '\u53d6\u6d88', '\u5173\u95ed'].some(text => label(el).includes(text)));
      if (target) clickElement(target);
    }}
    document.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
  }}

  function state() {{
    const body = document.body?.innerText || '';
    const relevant = allDeep()
      .filter(el => visible(el))
      .map((el, i) => ({{ i, tag: el.tagName, role: el.getAttribute?.('role') || null, text: label(el).slice(0, 180) }}))
      .filter(item => /Runtime type|Hardware accelerator|GPU|TPU|None|Save|Apply|Restart|\u8fd0\u884c\u65f6|\u786c\u4ef6\u52a0\u901f|\u4fdd\u5b58/i.test(item.text))
      .slice(0, 80);
    return {{
      href: location.href,
      hasGpuHint: /\b(T4|L4|A100|V100|GPU|TPU)\b/i.test(body),
      relevant,
    }};
  }}

  closeExistingDialogs();
  await sleep(400);
  out.before = state();
  let step = clickByLabels(runtimeMenuLabels);
  out.steps.push({{ name: 'open-runtime-menu', ...step }});
  await sleep(600);
  step = clickByLabels(runtimeDialogLabels);
  out.steps.push({{ name: 'open-runtime-dialog', ...step }});
  await sleep(1200);
  step = clickByLabels(acceleratorLabels);
  out.steps.push({{ name: 'open-accelerator-menu', ...step }});
  await sleep(800);
  step = clickByLabels(targetLabels, true);
  if (!step.clicked) step = clickByLabels(targetLabels);
  out.steps.push({{ name: 'choose-accelerator', ...step }});
  await sleep(500);
  out.dialogState = state();

  if (shouldApply) {{
    step = clickByLabels(applyLabels, true);
    if (!step.clicked) step = clickByLabels(applyLabels);
    out.steps.push({{ name: 'apply-runtime-dialog', ...step }});
    out.applied = Boolean(step.clicked);
    await sleep(1000);
    const confirm = clickByLabels(applyLabels, true);
    if (confirm.clicked) {{
      out.steps.push({{ name: 'confirm-runtime-change', ...confirm }});
      await sleep(1000);
    }}
  }}

  out.after = state();
  const failed = out.steps.filter(s => !s.clicked && !['confirm-runtime-change'].includes(s.name));
  out.ok = failed.length === 0 && (!shouldApply || out.applied);
  if (failed.length) out.warnings.push(`Some UI steps did not click: ${{failed.map(s => s.name).join(', ')}}`);
  if (out.applied) out.warnings.push('Changing accelerator restarts or disconnects the Colab runtime; reconnect may be required.');
  return JSON.stringify(out);
}})()
"""


def command_set_accelerator(args: argparse.Namespace) -> None:
    result = eval_colab(
        args.port,
        accelerator_script(args.accelerator, not args.no_apply),
        await_promise=True,
    )["result"]["value"]
    print(result)


def command_smoke_browser(args: argparse.Namespace) -> None:
    script = r"""
(async()=>{
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  const loginRequired = !email || email === 'anonymous' || /Sign in|\u767b\u5f55|\u767b\u5165/.test(body);
  if (loginRequired) return JSON.stringify({ok:false, loginRequired:true, email, message:'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'});
  const svc = window.colab?.global?.notebook?.colabMcpToolsService;
  if (!svc) return JSON.stringify({ok:false,error:'colabMcpToolsService missing'});
  const tool = n => svc.tools.find(t => t.toolName === n);
  const before = await tool('get_cells').toolCallback({includeOutputs:false});
  const add = await tool('add_code_cell').toolCallback({cellIndex: before.cells.length, language:'python', code:'x = 40 + 2\nprint(x)'});
  const id = add.newCellId;
  const run = await tool('run_code_cell').toolCallback({cellId:id});
  await tool('delete_cell').toolCallback({cellId:id});
  return JSON.stringify({ok:true, id, outputs: run.outputs});
})()
"""
    result = eval_colab(args.port, script, await_promise=True)["result"]["value"]
    print(result)


def command_smoke_mcp(args: argparse.Namespace) -> None:
    script = r"""
(async()=>{
  const body = document.body?.innerText || '';
  const email = window.colabUserEmail || null;
  const loginRequired = !email || email === 'anonymous' || /Sign in|\u767b\u5f55|\u767b\u5165/.test(body);
  if (loginRequired) return JSON.stringify({ok:false, loginRequired:true, email, message:'Colab login is required in the dedicated MCP browser. Ask the user to log into Google/Colab in that Edge window, then rerun the command.'});
  const local = window.colab?.global?.notebook?.localColabMcpService;
  if (!local) return JSON.stringify({ok:false,error:'localColabMcpService missing'});
  if (!local.isConnected()) {
    await local.connect();
  }
  const svc = window.colab?.global?.notebook?.colabMcpToolsService;
  if (!svc) return JSON.stringify({ok:false,error:'colabMcpToolsService missing', connected: local.isConnected()});
  const tool = n => svc.tools.find(t => t.toolName === n);
  const before = await tool('get_cells').toolCallback({includeOutputs:false});
  const add = await tool('add_code_cell').toolCallback({cellIndex: before.cells.length, language:'python', code:'print(42)'});
  const id = add.newCellId;
  const run = await tool('run_code_cell').toolCallback({cellId:id});
  await tool('delete_cell').toolCallback({cellId:id});
  const stdout = (run.outputs || []).flatMap(o => o.text || []).join('');
  return JSON.stringify({ok: stdout.includes('42'), connected: local.isConnected(), id, stdout, outputs: run.outputs});
})()
"""
    result = eval_colab(args.port, script, await_promise=True)["result"]["value"]
    print(result)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local browser/control helper for colab-mcp."
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_CDP_PORT,
        help="Edge CDP port. Defaults to the dedicated Colab MCP browser port.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status")
    status.set_defaults(func=command_status)

    connect = sub.add_parser("connect")
    connect.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    connect.add_argument("--timeout", type=float, default=120)
    connect.add_argument("--allow-ambiguous", action="store_true")
    connect.add_argument(
        "--url",
        help="Target notebook URL to open (e.g. https://colab.research.google.com/drive/xxx)",
        default=None,
    )
    connect.set_defaults(func=command_connect)

    eval_cmd = sub.add_parser("eval")
    eval_cmd.add_argument("expression", nargs="?")
    eval_cmd.add_argument("--file")
    eval_cmd.add_argument("--await-promise", action="store_true")
    eval_cmd.set_defaults(func=command_eval)

    smoke = sub.add_parser("smoke-browser")
    smoke.set_defaults(func=command_smoke_browser)

    smoke_mcp = sub.add_parser("smoke-mcp")
    smoke_mcp.set_defaults(func=command_smoke_mcp)

    list_tabs = sub.add_parser("list-tabs")
    list_tabs.set_defaults(func=command_list_tabs)

    snapshot = sub.add_parser("snapshot")
    snapshot.set_defaults(func=command_snapshot)

    click_text = sub.add_parser("click-text")
    click_text.add_argument("text")
    click_text.set_defaults(func=command_click_text)

    keepalive = sub.add_parser("keepalive")
    keepalive.add_argument(
        "--interval",
        type=float,
        default=300,
        help="Seconds between keepalive interactions.",
    )
    keepalive.add_argument(
        "--count",
        type=int,
        default=1,
        help="Number of keepalive interactions when --forever is not set.",
    )
    keepalive.add_argument(
        "--forever", action="store_true", help="Run until interrupted."
    )
    keepalive.set_defaults(func=command_keepalive)

    set_accelerator = sub.add_parser("set-accelerator")
    set_accelerator.add_argument(
        "--accelerator",
        default="GPU",
        help="Accelerator option to choose, e.g. GPU, T4 GPU, TPU, None.",
    )
    set_accelerator.add_argument(
        "--no-apply",
        action="store_true",
        help="Select the option but do not press Save/Apply.",
    )
    set_accelerator.set_defaults(func=command_set_accelerator)

    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    try:
        args.func(args)
    except Exception as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2),
            file=sys.stderr,
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
