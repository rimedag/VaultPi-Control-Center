(() => {
  const API = {
    settings: "/api/settings",
    projects: "/api/projects",
    actions: "/api/actions/commands",
    monitoringNow: "/api/monitoring/check-now",
  };

  let settings = {};
  let route = location.pathname;
  let lastToolbarPath = "";

  const helpText = {
    "/projects": "Projects are the master list. Add websites, local services, scripts, and notes here so they can appear in monitoring, local services, logs, and actions.",
    "/services/local": "Local services are projects running on this Raspberry Pi or your LAN. Add a local URL or port so VaultPi can monitor it.",
    "/services/remote": "Remote services are websites or external endpoints. Add a URL to track uptime and response status.",
    "/actions": "Quick actions run saved commands or scripts. Use them for repeatable maintenance tasks.",
    "/monitoring": "Monitoring watches projects with health checks enabled. Add websites or local services, then run a check to populate status history.",
    "/console": "Console runs one command at a time and returns the output.",
    "/terminal": "Terminal is a live shell session. Reset it if the shell gets into a strange state.",
    "/web-browser": "Web Browser uses w3m on the Pi to render lightweight text pages with a browser-like search bar.",
    "/settings": "Settings control modules, links, authentication, command execution, and optional help hints.",
  };

  const buttonHelp = {
    "new project": "Create a new project. Projects can be local services, remote websites, tools, or apps.",
    "add local service": "Add a service running locally or on your LAN. Use a local URL like http://10.0.0.5:8080.",
    "add website": "Add an external website or API endpoint to monitor.",
    "add monitor": "Add a website or local service with health monitoring enabled.",
    "check now": "Run all configured health checks immediately.",
    "add action": "Create a reusable command or shell script that can be run from Quick Actions.",
    "run": "Execute this command or action now.",
    "reset session": "Restart the live terminal session.",
    "save configuration": "Save the settings shown on this page.",
  };

  function el(tag, attrs = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    for (const child of Array.isArray(children) ? children : [children]) {
      if (child == null) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  async function fetchJson(url, options = {}) {
    const res = await fetch(url, {
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `Request failed: ${res.status}`);
    return data;
  }

  function toast(message, error = false) {
    document.querySelectorAll(".vp-toast").forEach((node) => node.remove());
    const node = el("div", { class: `vp-toast ${error ? "vp-toast-error" : ""}`, text: message });
    document.body.append(node);
    setTimeout(() => node.remove(), 4200);
  }

  function openModal(title, body, onSubmit) {
    const form = el("form", { class: "vp-modal-card" }, [
      el("div", { class: "vp-modal-head" }, [
        el("h2", { text: title }),
        el("button", { type: "button", class: "vp-icon-btn", "aria-label": "Close", onclick: () => overlay.remove() }, "x"),
      ]),
      body,
      el("div", { class: "vp-modal-actions" }, [
        el("button", { type: "button", class: "vp-btn-secondary", onclick: () => overlay.remove() }, "Cancel"),
        el("button", { type: "submit", class: "vp-btn-primary" }, "Save"),
      ]),
    ]);
    const overlay = el("div", { class: "vp-modal" }, form);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await onSubmit(new FormData(form));
        overlay.remove();
      } catch (error) {
        toast(error.message || "Save failed", true);
      }
    });
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) overlay.remove();
    });
    document.body.append(overlay);
    setTimeout(() => form.querySelector("input,textarea,select")?.focus(), 0);
  }

  function field(label, name, attrs = {}) {
    return el("label", { class: "vp-field" }, [
      el("span", { text: label }),
      attrs.type === "textarea"
        ? el("textarea", { name, rows: attrs.rows || "5", placeholder: attrs.placeholder || "" })
        : el("input", { name, type: attrs.type || "text", placeholder: attrs.placeholder || "", value: attrs.value || "" }),
    ]);
  }

  function selectField(label, name, options) {
    return el("label", { class: "vp-field" }, [
      el("span", { text: label }),
      el("select", { name }, options.map((item) => el("option", { value: item.value, text: item.label }))),
    ]);
  }

  function openProjectModal(defaults = {}) {
    const typeOptions = [
      { value: defaults.type || "remote app", label: defaults.type === "local app" ? "Local service" : "Remote website" },
      { value: "remote app", label: "Remote website" },
      { value: "local app", label: "Local app/service" },
      { value: "tool", label: "Tool" },
      { value: "service", label: "System service" },
    ];
    const body = el("div", { class: "vp-form-grid" }, [
      field("Name", "name", { placeholder: "My service" }),
      selectField("Type", "type", typeOptions),
      field("Local URL", "localUrl", { placeholder: "http://my-service.local:8000", value: defaults.localUrl || "" }),
      field("Remote URL", "remoteUrl", { placeholder: "https://example.com", value: defaults.remoteUrl || "" }),
      field("Healthcheck URL", "healthcheckUrl", { placeholder: "Optional, defaults to URL above" }),
      field("Port", "port", { type: "number", placeholder: "8000" }),
      field("Run command", "runCommand", { placeholder: "sudo systemctl start my-service" }),
      field("Restart command", "restartCommand", { placeholder: "sudo systemctl restart my-service" }),
      field("Log path", "logPath", { placeholder: "/var/log/my-service.log" }),
      field("Notes", "notes", { type: "textarea", rows: "3", placeholder: "What this service does" }),
    ]);
    openModal(defaults.title || "Add Project", body, async (form) => {
      const payload = Object.fromEntries(form.entries());
      payload.name = String(payload.name || "").trim();
      payload.healthcheckUrl = payload.healthcheckUrl || payload.localUrl || payload.remoteUrl;
      payload.monitoringEnabled = true;
      payload.actionEnabled = Boolean(payload.runCommand || payload.restartCommand);
      if (!payload.name) throw new Error("Name is required");
      await fetchJson(API.projects, { method: "POST", body: JSON.stringify(payload) });
      toast("Project added");
      setTimeout(() => location.reload(), 500);
    });
  }

  function openActionModal() {
    const scriptBox = field("Script content", "scriptContent", { type: "textarea", rows: "8", placeholder: "#!/bin/bash\nset -e\necho hello" });
    const fileInput = el("label", { class: "vp-field" }, [
      el("span", { text: "Load script file" }),
      el("input", { name: "scriptFile", type: "file", accept: ".sh,.bash,.txt" }),
    ]);
    fileInput.querySelector("input").addEventListener("change", async (event) => {
      const file = event.target.files?.[0];
      if (file) scriptBox.querySelector("textarea").value = await file.text();
    });
    const body = el("div", { class: "vp-form-grid" }, [
      field("Action name", "name", { placeholder: "Restart my service" }),
      field("Description", "description", { placeholder: "What this action does" }),
      field("One-line command", "command", { placeholder: "sudo systemctl restart my-service" }),
      field("Working directory", "workingDirectory", { placeholder: "/opt/my-service" }),
      field("Timeout seconds", "timeoutSec", { type: "number", value: "60" }),
      fileInput,
      scriptBox,
    ]);
    openModal("Add Action", body, async (form) => {
      const payload = Object.fromEntries(form.entries());
      delete payload.scriptFile;
      if (!String(payload.name || "").trim()) throw new Error("Action name is required");
      if (!String(payload.command || "").trim() && !String(payload.scriptContent || "").trim()) {
        throw new Error("Add either a command or script content");
      }
      await fetchJson(API.actions, { method: "POST", body: JSON.stringify(payload) });
      toast("Action added");
      setTimeout(() => location.reload(), 500);
    });
  }

  function makeButton(label, onClick) {
    return el("button", { type: "button", class: "vp-btn-primary", onclick: onClick }, label);
  }

  function ensureCreatorCredit() {
    if (document.querySelector(".vp-creator-credit")) return;
    document.body.append(el("a", {
      class: "vp-creator-credit",
      href: "",
      target: "_blank",
      rel: "noreferrer",
      text: "Built with VaultPi",
    }));
  }

  function ensureToolbar() {
    const path = location.pathname;
    const existing = document.querySelector(".vp-route-toolbar");
    if (existing?.dataset.path === path) return;
    document.querySelectorAll(".vp-route-toolbar").forEach((node) => node.remove());
    lastToolbarPath = path;
    const actions = [];
    if (path === "/projects") actions.push(makeButton("New Project", () => openProjectModal({ title: "New Project" })));
    if (path === "/services/local") actions.push(makeButton("Add Local Service", () => openProjectModal({ title: "Add Local Service", type: "local app" })));
    if (path === "/services/remote") {
      actions.push(makeButton("Add Website", () => openProjectModal({ title: "Add Website", type: "remote app" })));
      actions.push(makeButton("Add Local Service", () => openProjectModal({ title: "Add Local Service", type: "local app" })));
    }
    if (path === "/monitoring") {
      actions.push(makeButton("Add Monitor", () => openProjectModal({ title: "Add Monitoring Target", type: "remote app" })));
      actions.push(makeButton("Check Now", async () => {
        try {
          await fetchJson(API.monitoringNow, { method: "POST", body: "{}" });
          toast("Monitoring check started");
          setTimeout(() => location.reload(), 900);
        } catch (error) {
          toast(error.message || "Monitoring check failed", true);
        }
      }));
    }
    if (path === "/actions") actions.push(makeButton("Add Action", openActionModal));
    if (!actions.length) return;

    const toolbar = el("div", { class: "vp-route-toolbar", "data-path": path }, [
      el("div", { class: "vp-route-copy" }, [
        el("strong", { text: path === "/services/local" ? "Nothing here yet?" : "Quick add" }),
        el("span", { text: path === "/services/local" ? " Add a local service so this page has something useful to show." : " Create items without leaving this page." }),
      ]),
      el("div", { class: "vp-route-actions" }, actions),
    ]);
    const root = document.getElementById("root");
    root?.append(toolbar);
  }

  function setupHelp() {
    const enabled = settings.help_mode === "1" || settings.help_mode === true;
    const currentHelp = document.body.dataset.vpHelpRendered;
    const nextHelp = `${location.pathname}:${enabled}`;
    if (currentHelp === nextHelp && document.querySelector(".vp-help-pill,.vp-settings-help,.vp-help-tip")) return;
    document.body.dataset.vpHelpRendered = nextHelp;
    document.body.classList.toggle("vp-help-on", enabled);
    document.querySelectorAll(".vp-help-pill,.vp-help-tip,.vp-settings-help").forEach((node) => node.remove());

    if (location.pathname === "/settings") {
      const panel = el("div", { class: "vp-settings-help" }, [
        el("label", { class: "vp-help-toggle" }, [
          el("input", { type: "checkbox", ...(enabled ? { checked: "checked" } : {}) }),
          el("span", { text: "Help popups" }),
        ]),
        el("p", { text: "When enabled, VaultPi adds small hints to pages, buttons, and controls." }),
      ]);
      panel.querySelector("input").addEventListener("change", async (event) => {
        settings.help_mode = event.target.checked ? "1" : "0";
        await fetchJson(API.settings, { method: "POST", body: JSON.stringify({ help_mode: event.target.checked }) });
        setupHelp();
        toast("Help setting saved");
      });
      document.getElementById("root")?.append(panel);
    }

    if (!enabled) return;
    const pageText = helpText[location.pathname];
    if (pageText) document.getElementById("root")?.append(el("div", { class: "vp-help-pill", text: pageText }));

    document.querySelectorAll("button,a,input,select,textarea").forEach((node) => {
      if (node.closest(".vp-modal,.vp-route-toolbar,.vp-settings-help")) return;
      const label = (node.innerText || node.getAttribute("aria-label") || node.placeholder || node.name || "").trim().toLowerCase();
      const text = Object.entries(buttonHelp).find(([key]) => label.includes(key))?.[1];
      if (!text || node.dataset.vpHelp) return;
      node.dataset.vpHelp = "1";
      const tip = el("span", { class: "vp-help-tip", title: text, text: "?" });
      node.insertAdjacentElement("afterend", tip);
    });
  }

  function ensureCardputerSettingsPanel() {
    const root = document.getElementById("root");
    const existing = document.querySelector("[data-vp-cardputer-settings]");
    if (location.pathname !== "/settings") {
      existing?.remove();
      return;
    }
    if (!root) return;
    existing?.remove();

    const hostInput = el("input", { type: "text", value: settings.cardputer_host || "", placeholder: "leave blank to use this Pi IP" });
    const portInput = el("input", { type: "number", value: settings.cardputer_api_port || "8001", min: "1", max: "65535" });
    const passInput = el("input", { type: "text", value: settings.cardputer_password || "password", placeholder: "password" });
    const apiPreview = el("code", {});
    const browserPreview = el("code", {});

    function refreshPreview() {
      const host = hostInput.value.trim() || "this-pi-ip";
      const port = portInput.value.trim() || "8001";
      const browserPort = settings.bind_port || "8000";
      apiPreview.textContent = `http://${host}:${port}`;
      browserPreview.textContent = `http://${host}:${browserPort}/cardputer`;
    }

    hostInput.addEventListener("input", refreshPreview);
    portInput.addEventListener("input", refreshPreview);

    const panel = el("div", { class: "vp-cardputer-settings", "data-vp-cardputer-settings": "1" }, [
      el("div", { class: "vp-cardputer-settings-grid" }, [
        el("label", { class: "vp-field" }, [el("span", { text: "Cardputer Host Override" }), hostInput]),
        el("label", { class: "vp-field" }, [el("span", { text: "Cardputer API Port" }), portInput]),
        el("label", { class: "vp-field" }, [el("span", { text: "Cardputer Password" }), passInput]),
      ]),
      el("div", { class: "vp-cardputer-preview" }, [
        el("strong", { text: "Cardputer target" }),
        apiPreview,
        el("span", { text: "Browser UI" }),
        browserPreview,
      ]),
      el("div", { class: "vp-modal-actions", style: "margin-top:.75rem;justify-content:flex-start" }, [
        el("button", { type: "button", class: "vp-btn-primary", onclick: async () => {
          const payload = {
            cardputer_host: hostInput.value.trim(),
            cardputer_api_port: portInput.value.trim() || "8001",
            cardputer_password: passInput.value.trim() || "password",
          };
          await fetchJson(API.settings, { method: "POST", body: JSON.stringify(payload) });
          settings = { ...settings, ...payload };
          refreshPreview();
          toast("Cardputer settings saved");
        } }, "Save Cardputer Link"),
      ]),
    ]);
    root.append(panel);
    refreshPreview();
  }

  function installClickInterceptors() {
    document.addEventListener("click", (event) => {
      const target = event.target.closest("button,a");
      if (!target || target.closest(".vp-modal,.vp-route-toolbar,.vp-settings-help")) return;
      const label = (target.innerText || target.getAttribute("aria-label") || "").trim().toLowerCase();
      if (location.pathname === "/projects" && label.includes("new project")) {
        event.preventDefault();
        event.stopPropagation();
        openProjectModal({ title: "New Project" });
      }
      if (location.pathname === "/actions" && label.includes("add action")) {
        event.preventDefault();
        event.stopPropagation();
        openActionModal();
      }
    }, true);
  }

  function installStyles() {
    if (document.getElementById("vp-spa-patch-style")) return;
    document.head.append(el("style", { id: "vp-spa-patch-style", text: `
      .vp-route-toolbar,.vp-settings-help,.vp-cardputer-settings,.vp-help-pill{position:fixed;right:1rem;z-index:60;max-width:min(460px,calc(100vw - 2rem));border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));box-shadow:0 16px 40px #0006;border-radius:10px}
      .vp-creator-credit{position:fixed;right:1rem;top:4.75rem;z-index:50;border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--muted-foreground));border-radius:8px;padding:.42rem .6rem;font-size:.78rem;font-weight:750;text-decoration:none;box-shadow:0 10px 24px #0005}
      .vp-creator-credit:hover{color:hsl(var(--foreground));border-color:hsl(var(--primary))}
      .vp-route-toolbar{bottom:1rem;display:flex;gap:1rem;align-items:center;justify-content:space-between;padding:.9rem}
      .vp-settings-help{bottom:1rem;padding:1rem}
      .vp-cardputer-settings{bottom:8.5rem;padding:1rem}
      .vp-settings-help p,.vp-route-copy span{color:hsl(var(--muted-foreground));font-size:.82rem}
      .vp-route-actions{display:flex;gap:.5rem;flex-wrap:wrap;justify-content:flex-end}
      .vp-btn-primary,.vp-btn-secondary,.vp-icon-btn{border-radius:8px;padding:.55rem .8rem;font-weight:700;cursor:pointer}
      .vp-btn-primary{background:hsl(var(--primary));color:hsl(var(--primary-foreground))}
      .vp-btn-secondary,.vp-icon-btn{border:1px solid hsl(var(--border));background:hsl(var(--background));color:hsl(var(--foreground))}
      .vp-modal{position:fixed;inset:0;z-index:100;background:#000b;display:grid;place-items:center;padding:1rem}
      .vp-modal-card{width:min(720px,100%);max-height:min(820px,92vh);overflow:auto;border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));border-radius:10px;box-shadow:0 22px 70px #000b;padding:1rem}
      .vp-modal-head,.vp-modal-actions{display:flex;align-items:center;justify-content:space-between;gap:.75rem}
      .vp-modal-head h2{font-size:1.2rem;font-weight:800}
      .vp-form-grid{display:grid;gap:.8rem;margin:1rem 0}
      .vp-field{display:grid;gap:.35rem;font-size:.9rem;font-weight:650}
      .vp-field input,.vp-field textarea,.vp-field select{width:100%;border:1px solid hsl(var(--border));background:hsl(var(--background));color:hsl(var(--foreground));border-radius:8px;padding:.65rem}
      .vp-modal-actions{justify-content:flex-end}
      .vp-toast{position:fixed;left:50%;bottom:1rem;transform:translateX(-50%);z-index:120;background:hsl(var(--primary));color:hsl(var(--primary-foreground));border-radius:8px;padding:.7rem 1rem;font-weight:800;box-shadow:0 14px 34px #0008}
      .vp-toast-error{background:hsl(var(--destructive));color:hsl(var(--destructive-foreground))}
      .vp-help-pill{top:1rem;right:1rem;padding:.85rem 1rem;font-size:.9rem;line-height:1.4}
      .vp-help-tip{display:inline-flex;align-items:center;justify-content:center;width:1.1rem;height:1.1rem;margin-left:.35rem;border-radius:999px;background:hsl(var(--primary));color:hsl(var(--primary-foreground));font-size:.72rem;font-weight:900;cursor:help}
      .vp-help-toggle{display:flex;align-items:center;gap:.55rem;font-weight:800}
      .vp-cardputer-settings-grid{display:grid;gap:.8rem}
      .vp-cardputer-preview{display:grid;gap:.25rem;margin-top:.5rem;color:hsl(var(--muted-foreground));font-size:.82rem}
      .vp-cardputer-preview code{color:hsl(var(--foreground));font-size:.8rem}
      @media (max-width: 720px){.vp-route-toolbar{left:1rem;align-items:flex-start;flex-direction:column}.vp-route-actions{justify-content:flex-start}.vp-help-pill{left:1rem;top:auto;bottom:5.5rem}.vp-creator-credit{top:auto;bottom:.75rem;left:1rem;right:auto}}
    ` }));
  }

  // ── Web Browser modal ──────────────────────────────────────────────────────

  function openWebBrowserModal(view, bookmarkData) {
    document.querySelectorAll(".vp-browser-modal").forEach((n) => n.remove());

    let content;
    if (view === "bookmarks") {
      const rows = (bookmarkData || []).map((bm) =>
        el("form", { method: "post", action: "/web-browser/bookmarks/open", class: "vp-bm-row" }, [
          el("input", { type: "hidden", name: "url", value: bm.url }),
          el("div", { class: "vp-bm-info" }, [
            el("strong", { text: bm.title }),
            el("span", { class: "vp-bm-url", text: bm.url }),
          ]),
          el("button", { type: "submit", class: "vp-btn-primary" }, "Open"),
        ])
      );
      content = el("div", {}, [
        el("div", { class: "vp-modal-head", style: "margin-bottom:.75rem" }, [
          el("h2", { text: "Bookmarks" }),
          el("button", { type: "button", class: "vp-icon-btn", "aria-label": "Back", onclick: () => openWebBrowserModal() }, "←"),
        ]),
        rows.length ? el("div", { class: "vp-bm-list" }, rows) : el("p", { text: "No bookmarks found.", style: "color:hsl(var(--muted-foreground))" }),
        el("p", { style: "margin-top:.75rem;font-size:.78rem;color:hsl(var(--muted-foreground))", text: "Edit: app/data/bookmarks.json on the Pi" }),
      ]);
    } else {
      content = el("div", {}, [
        el("div", { class: "vp-modal-head", style: "margin-bottom:.75rem" }, [
          el("h2", { text: "Web Browser (w3m)" }),
          el("button", { type: "button", class: "vp-icon-btn", "aria-label": "Close", onclick: () => overlay.remove() }, "×"),
        ]),
        el("form", { method: "post", action: "/web-browser/open" }, [
          el("label", { class: "vp-field" }, [
            el("span", { text: "Open URL" }),
            el("input", { name: "url", type: "url", placeholder: "https://example.com", autocomplete: "off", required: "" }),
          ]),
          el("div", { class: "vp-modal-actions", style: "margin-top:.5rem" }, [
            el("button", { type: "submit", class: "vp-btn-primary" }, "Open in w3m →"),
          ]),
        ]),
        el("hr", { style: "margin:.9rem 0;border:none;border-top:1px solid hsl(var(--border))" }),
        el("form", { method: "post", action: "/web-browser/search" }, [
          el("label", { class: "vp-field" }, [
            el("span", { text: "Search DuckDuckGo" }),
            el("input", { name: "query", placeholder: "raspberry pi tips", autocomplete: "off", required: "" }),
          ]),
          el("div", { class: "vp-modal-actions", style: "margin-top:.5rem" }, [
            el("button", { type: "submit", class: "vp-btn-primary" }, "Search →"),
          ]),
        ]),
        el("hr", { style: "margin:.9rem 0;border:none;border-top:1px solid hsl(var(--border))" }),
        el("div", { class: "vp-modal-actions" }, [
          el("button", { type: "button", class: "vp-btn-secondary", onclick: async () => {
            try {
              const bms = await fetchJson("/api/web-browser/bookmarks");
              openWebBrowserModal("bookmarks", bms);
            } catch {
              openWebBrowserModal("bookmarks", []);
            }
          }}, "Bookmarks"),
        ]),
      ]);
    }

    const card = el("div", { class: "vp-modal-card vp-browser-modal-card" }, [content]);
    const overlay = el("div", { class: "vp-modal vp-browser-modal" }, [card]);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    document.body.append(overlay);
    setTimeout(() => card.querySelector("input")?.focus(), 0);
  }

  function ensureWebBrowserNav() {
    if (document.querySelector("[data-vp-browser-nav]")) return;
    // Find the nav container — look for any sidebar link to borrow its parent and class
    const refLink = document.querySelector("nav a, aside a, [role='navigation'] a");
    if (!refLink) return;
    const navParent = refLink.closest("nav, aside, ul, [role='navigation']");
    if (!navParent) return;
    const link = el("a", {
      href: "/web-browser",
      "data-vp-browser-nav": "1",
      class: refLink.className,   // inherit React nav link classes
      onclick: (e) => {
        e.preventDefault();
        history.pushState({}, "", "/web-browser");
        const root = document.getElementById("root");
        if (root) delete root.dataset.vpBrowserRendered;
        maybeRenderBrowserPage();
      },
    }, "Web Browser");
    navParent.appendChild(link);
  }

  function installBrowserStyles() {
    if (document.getElementById("vp-browser-style")) return;
    document.head.append(el("style", { id: "vp-browser-style", text: `
      .vp-browser-modal-card { min-width: min(400px, 96vw); }
      .vp-bm-list { display: flex; flex-direction: column; gap: .45rem; max-height: 320px; overflow-y: auto; }
      .vp-bm-row { display: flex; align-items: center; gap: .6rem; padding: .45rem .5rem; border: 1px solid hsl(var(--border)); border-radius: 8px; }
      .vp-bm-info { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: .15rem; }
      .vp-bm-url { font-size: .75rem; color: hsl(var(--muted-foreground)); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .vp-browser-page{min-height:100vh;padding:1rem;display:flex;flex-direction:column;gap:.85rem;background:hsl(var(--background));color:hsl(var(--foreground))}
      .vp-browser-top{display:flex;justify-content:space-between;gap:1rem;align-items:flex-end}
      .vp-browser-title h1{font-size:1.35rem;font-weight:900;margin:0}
      .vp-browser-title p{margin:.2rem 0 0;color:hsl(var(--muted-foreground));font-size:.86rem}
      .vp-browser-status{border:1px solid hsl(var(--border));border-radius:8px;padding:.35rem .55rem;color:hsl(var(--muted-foreground));font:700 .78rem monospace;white-space:nowrap}
      .vp-browser-bar{display:grid;grid-template-columns:auto 1fr auto auto;gap:.5rem}
      .vp-browser-back{border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));border-radius:8px;padding:.7rem .75rem;font-size:1rem;cursor:pointer;line-height:1}
      .vp-browser-back:disabled{opacity:.35;cursor:default}
      .vp-browser-address{min-width:0;border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));border-radius:8px;padding:.7rem .8rem;font:700 .95rem system-ui}
      .vp-browser-bookmarks{display:flex;gap:.45rem;overflow-x:auto;padding-bottom:.1rem}
      .vp-bookmark-chip{border:1px solid hsl(var(--border));background:hsl(var(--card));color:hsl(var(--foreground));border-radius:999px;padding:.45rem .65rem;font-size:.82rem;font-weight:750;white-space:nowrap;cursor:pointer}
      .vp-browser-screen{flex:1;min-height:420px;border:1px solid hsl(var(--border));background:#05070a;color:#d7f7d0;border-radius:8px;overflow:auto;box-shadow:inset 0 0 0 1px #ffffff08}
      .vp-browser-output{margin:0;padding:1rem;white-space:pre-wrap;font:14px/1.45 Consolas,Monaco,"Courier New",monospace;color:#d7f7d0}
      .vp-browser-error{color:#ff9a9a}
      .vp-browser-empty,.vp-browser-loading{min-height:420px;display:grid;place-content:center;text-align:center;gap:.35rem;color:#7f8b99;font:700 .95rem system-ui}
      .vp-browser-empty span{font-size:.85rem;font-weight:500}
      .vp-browser-shortcut{margin:1.5rem 1rem 0;border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:10px;padding:1rem 1.2rem;box-shadow:0 4px 18px #0003;display:flex;gap:1rem;align-items:center;justify-content:space-between}
      .vp-browser-shortcut-copy{display:grid;gap:.25rem}
      .vp-browser-shortcut-copy strong{font-size:1rem}
      .vp-browser-shortcut-copy span{color:hsl(var(--muted-foreground));font-size:.84rem;max-width:50rem}
      .vp-browser-shortcut-actions{display:flex;gap:.5rem;flex-wrap:wrap}
      @media (max-width:720px){.vp-browser-page{padding:.75rem}.vp-browser-top{display:block}.vp-browser-bar{grid-template-columns:1fr}.vp-browser-screen,.vp-browser-empty,.vp-browser-loading{min-height:330px}.vp-browser-output{font-size:12px;padding:.75rem}}
    ` }));
  }

  async function loadBrowserBookmarks() {
    try { return await fetchJson("/api/web-browser/bookmarks"); }
    catch { return []; }
  }

  function browserLinesView(lines = []) {
    if (!lines.length) {
      return el("div", { class: "vp-browser-empty" }, [
        el("strong", { text: "Ready" }),
        el("span", { text: "Search or open a URL. Pages render as terminal text via w3m." }),
      ]);
    }
    return el("pre", { class: "vp-browser-output", text: lines.join("\n") });
  }

  function renderBrowserPage() {
    const root = document.getElementById("root");
    if (!root) return;
    if (root.dataset.vpBrowserRendered === "1") return;
    root.dataset.vpBrowserRendered = "1";
    root.innerHTML = "";
    let currentUrl = "";
    const navHistory = [];
    const output = el("div", { class: "vp-browser-screen" }, [browserLinesView()]);
    const status = el("span", { class: "vp-browser-status", text: "w3m text mode" });
    const address = el("input", { class: "vp-browser-address", placeholder: "Search or enter URL", autocomplete: "off" });
    const backBtn = el("button", { type: "button", class: "vp-browser-back", title: "Back", disabled: "" }, "←");
    backBtn.addEventListener("click", () => {
      const prev = navHistory.pop();
      if (prev) { openUrl(prev, false); }
      backBtn.disabled = navHistory.length === 0;
    });

    async function setResult(promise, loadingText, pushHistory = true) {
      status.textContent = loadingText;
      output.innerHTML = "";
      output.append(el("div", { class: "vp-browser-loading", text: loadingText }));
      try {
        const data = await promise;
        if (pushHistory && currentUrl) {
          navHistory.push(currentUrl);
          backBtn.disabled = false;
        }
        currentUrl = data.url || currentUrl;
        address.value = currentUrl;
        status.textContent = data.ok ? `${data.total || data.lines?.length || 0} lines` : (data.error || "failed");
        output.innerHTML = "";
        output.append(browserLinesView(data.lines || []));
      } catch (error) {
        status.textContent = error.message || "failed";
        output.innerHTML = "";
        output.append(el("pre", { class: "vp-browser-output vp-browser-error", text: error.message || String(error) }));
      }
    }

    function isProbablyUrl(value) {
      return /^https?:\/\//i.test(value) || /^[\w.-]+\.[a-z]{2,}([/:?#].*)?$/i.test(value);
    }
    function openUrl(url, pushHist = true) {
      const value = String(url || "").trim();
      if (!value) return;
      setResult(fetchJson("/api/web-browser/fetch", { method: "POST", body: JSON.stringify({ url: value, cols: 96 }) }), "Opening page...", pushHist);
    }
    function search(query, pushHist = true) {
      const value = String(query || "").trim();
      if (!value) return;
      setResult(fetchJson("/api/web-browser/search", { method: "POST", body: JSON.stringify({ query: value, cols: 96 }) }), "Searching...", pushHist);
    }
    function go() {
      const value = address.value.trim();
      if (!value) return;
      if (isProbablyUrl(value)) openUrl(value);
      else search(value);
    }

    const bookmarks = el("div", { class: "vp-browser-bookmarks" });
    const panel = el("div", { class: "vp-browser-page" }, [
      el("div", { class: "vp-browser-top" }, [
        el("div", { class: "vp-browser-title" }, [
          el("h1", { text: "Web Browser" }),
          el("p", { text: "Lightweight browsing powered by w3m on the Raspberry Pi." }),
        ]),
        status,
      ]),
      el("form", { class: "vp-browser-bar", onsubmit: (event) => { event.preventDefault(); go(); } }, [
        backBtn,
        address,
        el("button", { type: "submit", class: "vp-btn-primary" }, "Go"),
        el("button", { type: "button", class: "vp-btn-secondary", onclick: () => search(address.value) }, "Search"),
      ]),
      bookmarks,
      output,
    ]);
    root.append(panel);
    loadBrowserBookmarks().then((items) => {
      bookmarks.innerHTML = "";
      items.forEach((bm) => bookmarks.append(el("button", {
        type: "button", class: "vp-bookmark-chip", title: bm.url, onclick: () => openUrl(bm.url),
      }, bm.title || bm.url)));
    });
    setTimeout(() => address.focus(), 0);
  }

  function maybeRenderBrowserPage() {
    const root = document.getElementById("root");
    if (location.pathname === "/web-browser") renderBrowserPage();
    else if (root) delete root.dataset.vpBrowserRendered;
  }

  function ensureOverviewBrowserShortcut() {
    const isOverview = location.pathname === "/" || location.pathname === "";
    document.querySelectorAll("[data-vp-browser-shortcut]").forEach((node) => {
      if (!isOverview) node.remove();
    });
    if (!isOverview) return;
    if (document.querySelector("[data-vp-browser-shortcut]")) return;
    const root = document.getElementById("root");
    if (!root) return;
    const card = el("div", { "data-vp-browser-shortcut": "1", class: "vp-browser-shortcut" }, [
      el("div", { class: "vp-browser-shortcut-copy" }, [
        el("strong", { text: "Web Browser" }),
        el("span", { text: "Search the web or open lightweight text pages through w3m on the Raspberry Pi." }),
      ]),
      el("div", { class: "vp-browser-shortcut-actions" }, [
        el("button", {
          type: "button",
          class: "vp-btn-primary",
          onclick: () => {
            history.pushState({}, "", "/web-browser");
            const appRoot = document.getElementById("root");
            if (appRoot) delete appRoot.dataset.vpBrowserRendered;
            maybeRenderBrowserPage();
          },
        }, "Open Browser"),
      ]),
    ]);
    root.append(card);
  }

  // ── Cardputer monitor ──────────────────────────────────────────────────────

  let _cpmState = null;
  let _cpmTimer = null;

  async function pollCardputerMonitor() {
    try {
      _cpmState = await fetchJson("/api/cardputer/monitor");
    } catch (e) {
      _cpmState = { online: false, error: String(e) };
    }
    const card = document.querySelector("[data-vp-cpm]");
    if (card) renderCardputerMonitor(card);
  }

  function renderCardputerMonitor(card) {
    const s = _cpmState;
    card.innerHTML = "";
    const online = s && s.online;
    const dot = el("span", { class: `vp-cpm-dot ${online ? "vp-cpm-on" : "vp-cpm-off"}` });
    const head = el("div", { class: "vp-cpm-head" }, [
      el("span", { class: "vp-cpm-title", text: "M5Cardputer" }),
      dot,
      el("span", { class: `vp-cpm-badge ${online ? "vp-cpm-on" : "vp-cpm-off"}`, text: online ? "ONLINE" : "OFFLINE" }),
    ]);
    card.append(head);
    if (!s || !online) {
      card.append(el("p", { class: "vp-cpm-msg", text: s?.error || "Cardputer listener not reached" }));
      return;
    }
    const rows = [];
    if (s.effectiveHost) rows.push(["Host", s.effectiveHost]);
    if (s.apiPort != null) rows.push(["Port", String(s.apiPort)]);
    if (s.password) rows.push(["Password", s.password]);
    if (s.browserUrl) rows.push(["Browser", s.browserUrl]);
    if (s.responseMs != null) rows.push(["Check", `${s.responseMs} ms`]);
    if (s.screen)   rows.push(["Screen",   s.screen.toUpperCase()]);
    if (s.battery != null) rows.push(["Battery", `${s.battery >= 0 ? s.battery + "%" : "n/a"}${s.charging ? " ⚡" : ""}`]);
    if (s.rssi)     rows.push(["WiFi",     `${s.rssi} dBm`]);
    if (s.uptime)   rows.push(["Uptime",   s.uptime]);
    if (s.ip)       rows.push(["IP",       s.ip]);
    if (s.firmware) rows.push(["FW",       s.firmware]);
    if (s.age != null) rows.push(["Last seen", `${Math.round(s.age)}s ago`]);
    card.append(
      el("div", { class: "vp-cpm-grid" },
        rows.map(([k, v]) => [
          el("span", { class: "vp-cpm-k", text: k }),
          el("span", { class: "vp-cpm-v", text: v }),
        ]).flat()
      )
    );
  }

  function ensureCardputerMonitor() {
    const isOverview = location.pathname === "/" || location.pathname === "";
    const existing = document.querySelector("[data-vp-cpm]");
    if (!isOverview) {
      existing?.remove();
      if (_cpmTimer) { clearInterval(_cpmTimer); _cpmTimer = null; }
      return;
    }
    if (!existing) {
      const card = el("div", { "data-vp-cpm": "1", class: "vp-cpm-card" });
      renderCardputerMonitor(card);
      document.getElementById("root")?.append(card);
      pollCardputerMonitor();
      if (!_cpmTimer) _cpmTimer = setInterval(pollCardputerMonitor, 15000);
    }
  }

  function installCardputerMonitorStyles() {
    if (document.getElementById("vp-cpm-style")) return;
    document.head.append(el("style", { id: "vp-cpm-style", text: `
      .vp-cpm-card{margin:1.5rem 1rem 0;border:1px solid hsl(var(--border));background:hsl(var(--card));border-radius:10px;padding:1rem 1.2rem;box-shadow:0 4px 18px #0003}
      .vp-cpm-head{display:flex;align-items:center;gap:.5rem;margin-bottom:.7rem}
      .vp-cpm-title{font-weight:800;font-size:.95rem}
      .vp-cpm-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
      .vp-cpm-badge{font-size:.72rem;font-weight:700;letter-spacing:.05em;padding:.15rem .45rem;border-radius:4px}
      .vp-cpm-on{background:#22c55e33;color:#22c55e}
      .vp-cpm-off{background:#ef444433;color:#ef4444}
      .vp-cpm-dot.vp-cpm-on{background:#22c55e;box-shadow:0 0 6px #22c55e88}
      .vp-cpm-dot.vp-cpm-off{background:#ef4444;box-shadow:0 0 4px #ef444466}
      .vp-cpm-grid{display:grid;grid-template-columns:5rem 1fr;gap:.25rem .75rem;font-size:.85rem}
      .vp-cpm-k{color:hsl(var(--muted-foreground));font-size:.78rem;padding-top:.1rem}
      .vp-cpm-v{font-weight:600;font-family:monospace}
      .vp-cpm-msg{color:hsl(var(--muted-foreground));font-size:.82rem;margin-top:.3rem}
    ` }));
  }

  // ── Boot ────────────────────────────────────────────────────────────────────

  async function boot() {
    installStyles();
    installBrowserStyles();
    installCardputerMonitorStyles();
    installClickInterceptors();
    ensureCreatorCredit();
    try {
      settings = await fetchJson(API.settings);
    } catch {
      settings = {};
    }
    ensureToolbar();
    ensureCardputerSettingsPanel();
    ensureWebBrowserNav();
    ensureOverviewBrowserShortcut();
    maybeRenderBrowserPage();
    ensureCardputerMonitor();
    setupHelp();
    setInterval(() => {
      if (route !== location.pathname) {
        route = location.pathname;
        setTimeout(() => {
          ensureToolbar();
          ensureCardputerSettingsPanel();
          ensureOverviewBrowserShortcut();
          ensureCardputerMonitor();
          maybeRenderBrowserPage();
          setupHelp();
        }, 120);
      }
    }, 250);
    new MutationObserver((mutations) => {
      const onlyPatchNodes = mutations.every((mutation) =>
        [...mutation.addedNodes, ...mutation.removedNodes].every((node) =>
          node.nodeType !== 1 || node.classList?.contains("vp-route-toolbar") ||
          node.classList?.contains("vp-settings-help") ||
          node.classList?.contains("vp-cardputer-settings") ||
          node.classList?.contains("vp-help-pill") ||
          node.classList?.contains("vp-help-tip") ||
          node.closest?.(".vp-route-toolbar,.vp-settings-help,.vp-cardputer-settings,.vp-help-pill,.vp-modal")
        )
      );
      if (onlyPatchNodes) return;
      ensureToolbar();
      ensureCardputerSettingsPanel();
      ensureWebBrowserNav();
      ensureOverviewBrowserShortcut();
      maybeRenderBrowserPage();
      ensureCardputerMonitor();
      setupHelp();
    }).observe(document.getElementById("root") || document.body, { childList: true, subtree: true });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
