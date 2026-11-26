/* Memoria: sign in, albums, create, join and invite. Screens follow docs/UI_SPEC.md.
   Everything people type is put on the page as text, never as HTML. */
(function () {
  "use strict";

  var SESSION = "album-session";        // shared with the /upload and /people pages
  var INVITE = "memoria-invite";        // an invite link opened before signing in
  var INVITE_MAX_AGE = 24 * 3600;
  var OFFLINE = "No internet connection. Check it and try again.";
  var GENERIC = "Something went wrong. Please try again.";

  // ------------------------------------------------------------------ pure helpers (tested with Node)

  // What the address that opened the page is asking for.
  function parseEntry(pathname, hash) {
    var raw = (hash || "").replace(/^#/, "");
    if (/(^|&)(access_token|error_description)=/.test(raw)) {
      var p = new URLSearchParams(raw);
      if (p.get("access_token")) {
        return { auth: { access_token: p.get("access_token"), refresh_token: p.get("refresh_token"),
                         expires_in: p.get("expires_in") }, type: p.get("type"), route: "" };
      }
      return { error: p.get("error_description"), route: "" };
    }
    if (pathname === "/join") {
      return /^[A-Za-z0-9_-]{20,200}$/.test(raw) ? { invite: raw, route: "" } : { badInvite: true, route: "" };
    }
    return { route: raw.indexOf("/") === 0 ? raw : "" };
  }

  // Plain words for what the sign-in service says went wrong.
  function authMessage(status, body) {
    var code = String((body && (body.error_code || body.code || body.error)) || "");
    var text = String((body && (body.msg || body.message || body.error_description)) || "");
    var all = (code + " " + text).toLowerCase();
    if (status === 429 || all.indexOf("rate limit") >= 0) return "Too many tries. Wait a few minutes, then try again.";
    if (all.indexOf("not_confirmed") >= 0 || all.indexOf("not confirmed") >= 0) {
      return "Check your email and tap the link we sent, then sign in.";
    }
    if (all.indexOf("invalid_credentials") >= 0 || all.indexOf("invalid login") >= 0) {
      return "That email and password don't match. Try again.";
    }
    if (all.indexOf("already") >= 0 || all.indexOf("email_exists") >= 0) {
      return "You already have an account. Sign in instead.";
    }
    if (all.indexOf("same_password") >= 0 || all.indexOf("different from the old") >= 0) {
      return "Choose a password you haven't used before.";
    }
    if (all.indexOf("weak") >= 0 || all.indexOf("password should") >= 0) {
      return "Choose a longer or less common password.";
    }
    if (all.indexOf("email") >= 0 && all.indexOf("invalid") >= 0) return "Check the email address and try again.";
    return GENERIC;
  }

  // The app's own server, in the words the screens use ("album", not "library").
  function apiMessage(status, detail) {
    if (detail === "A library must keep at least one owner") {
      return "You're the only owner. Make someone else an owner first.";
    }
    if (detail === "Library not found") return "This album isn't available to you any more.";
    if (status === 403) return "You don't have permission to do that.";
    if (typeof detail === "string" && status < 500) return detail;
    return GENERIC;
  }

  // Group photos by the month they were taken, using the local wall-clock date (never converted to UTC).
  function byMonth(items) {
    var names = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
                 "October", "November", "December"];
    var groups = [], seen = {}, undated = [];
    items.forEach(function (item) {
      var m = /^(\d{4})-(\d{2})/.exec(item.taken_local || "");
      if (!m) { undated.push(item); return; }
      var label = names[Number(m[2]) - 1] + " " + m[1];
      if (!Object.prototype.hasOwnProperty.call(seen, label)) {
        seen[label] = groups.length;
        groups.push({ label: label, items: [] });
      }
      groups[seen[label]].items.push(item);
    });
    if (undated.length) groups.push({ label: "No date", items: undated });
    return groups;
  }

  function photoCount(n) { return n === 1 ? "1 photo" : n.toLocaleString("en-US") + " photos"; }

  function noFilters() { return { people: [], match: "all", when: { kind: "any" }, uploaders: [] }; }

  // The chosen filters as query parameters ("" = nothing chosen). `today` is this device's date as MM-DD.
  function filterQuery(f, today) {
    var q = new URLSearchParams(), w = f.when;
    f.people.forEach(function (id) { q.append("person", id); });
    if (f.people.length > 1 && f.match === "any") q.set("match", "any");
    if (w.kind === "year") q.set("year", w.year);
    if (w.kind === "range") {
      if (w.from) q.set("date_from", w.from);
      if (w.to) q.set("date_to", w.to);
    }
    if (w.kind === "undated") q.set("undated", "true");
    if (w.kind === "today") q.set("on_day", today);
    f.uploaders.forEach(function (id) { q.append("uploader", id); });
    return q.toString();
  }

  // What is being filtered, in words, e.g. "Mia and Leo · 2019" ("" = nothing). `names` maps ids to names.
  function filterSummary(f, names) {
    function list(ids, joiner) {
      if (ids.length > 2) return ids.length + " people";
      return ids.map(function (id) { return names[id] || "1 person"; }).join(joiner);
    }
    var w = f.when, date = "";
    if (w.kind === "year") date = String(w.year);
    if (w.kind === "undated") date = "No date";
    if (w.kind === "today") date = "On this day";
    if (w.kind === "range" && (w.from || w.to)) date = "Chosen dates";
    return [list(f.people, f.match === "any" ? " or " : " and "), date,
            f.uploaders.length ? "Added by " + list(f.uploaders, " or ") : ""].filter(Boolean).join(" · ");
  }

  // The square of the original photo to show for a face (bbox = [left, top, right, bottom] in photo pixels).
  function faceBox(bbox, width, height) {
    if (!width || !height || !bbox || bbox.length < 4) return null;
    var side = Math.min(Math.max(bbox[2] - bbox[0], bbox[3] - bbox[1]) * 1.7, width, height);
    return { x: Math.min(Math.max(0, (bbox[0] + bbox[2]) / 2 - side / 2), width - side),
             y: Math.min(Math.max(0, (bbox[1] + bbox[3]) / 2 - side / 2), height - side), side: side };
  }

  if (typeof module !== "undefined") {
    module.exports = { parseEntry: parseEntry, authMessage: authMessage, apiMessage: apiMessage, byMonth: byMonth,
                       photoCount: photoCount, noFilters: noFilters, filterQuery: filterQuery,
                       filterSummary: filterSummary, faceBox: faceBox };
  }
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------------ building the page

  var root = document.getElementById("app");
  var cfg = null, flash = null, drawing = 0, firstDraw = true, renewing = null;
  var filters = {}, names = {};  // per album: what is being filtered; id -> name, for the button labels

  function el(tag, props) {
    var n = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === null || v === undefined || v === false) return;
      if (k === "class") n.className = v;
      else if (k.slice(0, 2) === "on") n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    });
    Array.prototype.slice.call(arguments, 2).forEach(function add(c) {
      if (Array.isArray(c)) c.forEach(add);
      else if (c !== null && c !== undefined && c !== false) n.append(c);
    });
    return n;
  }

  var ICONS = {
    back: ["M15 5l-7 7 7 7"],
    plus: ["M12 5v14M5 12h14"],
    invite: ["M10 5a3.5 3.5 0 110 7 3.5 3.5 0 010-7z", "M3.5 19c.8-3.2 3.2-5 6.5-5s5.7 1.8 6.5 5", "M19 8v6M16 11h6"],
    gear: ["M12 5.5a6.5 6.5 0 110 13 6.5 6.5 0 010-13z", "M12 9.7a2.3 2.3 0 110 4.6 2.3 2.3 0 010-4.6z",
           "M12 2.5v3M12 18.5v3M2.5 12h3M18.5 12h3M5.3 5.3l2.1 2.1M16.6 16.6l2.1 2.1M18.7 5.3l-2.1 2.1M7.4 16.6l-2.1 2.1"],
    photo: ["M6.5 4.5h11a3 3 0 013 3v9a3 3 0 01-3 3h-11a3 3 0 01-3-3v-9a3 3 0 013-3z", "M4 17l5-4.5 3.5 3 3-2.5 4.5 4"],
    next: ["M9 5l7 7-7 7"],
    filter: ["M4 7h16M7 12h10M10 17h4"]
  };

  function icon(name, size) {
    var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    var attrs = { width: size || 24, height: size || 24, viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
                  "stroke-width": "2.2", "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true" };
    Object.keys(attrs).forEach(function (k) { svg.setAttribute(k, attrs[k]); });
    ICONS[name].forEach(function (d) {
      var p = document.createElementNS(NS, "path");
      p.setAttribute("d", d);
      svg.append(p);
    });
    return svg;
  }

  function back(href, label) { return el("a", { class: "link", href: href }, icon("back", 22), label || "Back"); }
  function iconLink(href, name, label) { return el("a", { class: "round", href: href, "aria-label": label }, icon(name, 26)); }
  function button(label, props) { return el("button", Object.assign({ type: "button", class: "btn" }, props), label); }

  function field(id, label, props, hint) {
    var input = el("input", Object.assign({ id: id, name: id, class: "input" }, props));
    return { input: input, node: el("div", {}, el("label", { for: id }, label), input,
                                    hint ? el("div", { class: "hint" }, hint) : null) };
  }

  // A form whose button is disabled while working, with the error shown next to it. Typed text is kept.
  function form(fields, submitLabel, work, extras) {
    var error = el("p", { class: "error", role: "alert" });
    var go = el("button", { type: "submit", class: "btn primary" }, submitLabel);
    var node = el("form", { class: "stack", novalidate: true },
                  fields.map(function (f) { return f.node; }), error, go, extras || null);
    node.addEventListener("submit", async function (e) {
      e.preventDefault();
      if (go.disabled) return;
      error.textContent = "";
      go.disabled = true;
      try { await work(); }
      catch (problem) { error.textContent = problem.friendly || GENERIC; if (!problem.friendly) console.error(problem); }
      finally { go.disabled = false; }
    });
    return node;
  }

  function Problem(message, status) { var e = new Error(message); e.friendly = message; e.status = status; return e; }

  // ------------------------------------------------------------------ signing in

  function stored(key) { try { return JSON.parse(localStorage.getItem(key)); } catch (e) { return null; } }
  function now() { return Math.floor(Date.now() / 1000); }

  function adopt(p) {
    localStorage.setItem(SESSION, JSON.stringify({ access_token: p.access_token, refresh_token: p.refresh_token,
      expires_at: now() + (parseInt(p.expires_in, 10) || 3600) }));
  }

  function pendingInvite() {
    var i = stored(INVITE);
    if (i && i.token && now() - i.at < INVITE_MAX_AGE) return i.token;
    localStorage.removeItem(INVITE);
    return null;
  }

  async function config() {
    if (cfg) return cfg;
    var r;
    try { r = await fetch("/api/config"); } catch (e) { throw Problem(OFFLINE); }
    if (!r.ok) throw Problem(GENERIC, r.status);
    cfg = await r.json();
    return cfg;
  }

  // Talk to the sign-in service (Supabase). The password goes straight there, never to our server.
  async function authCall(method, path, body, token) {
    var c = await config();
    var headers = { apikey: c.supabase_anon_key, "Content-Type": "application/json" };
    if (token) headers.Authorization = "Bearer " + token;
    var r;
    try {
      r = await fetch(c.supabase_url + "/auth/v1/" + path,
                      { method: method, headers: headers, body: body ? JSON.stringify(body) : undefined });
    } catch (e) { throw Problem(OFFLINE); }
    var data = await r.json().catch(function () { return {}; });
    if (!r.ok) throw Problem(authMessage(r.status, data), r.status);
    return data;
  }

  // Renew the hour-long sign-in quietly. Only one renewal at a time: each refresh token works once.
  function renew() {
    if (!renewing) {
      renewing = (async function () {
        var s = stored(SESSION);
        if (!s || !s.refresh_token) return false;
        try { adopt(await authCall("POST", "token?grant_type=refresh_token", { refresh_token: s.refresh_token })); }
        catch (e) {
          if (e.status >= 400 && e.status < 500 && e.status !== 429) { localStorage.removeItem(SESSION); return false; }
          throw e;
        }
        return true;
      })().finally(function () { renewing = null; });
    }
    return renewing;
  }

  function signedOut() { localStorage.removeItem(SESSION); go("#/signin"); return Problem("Please sign in again.", 401); }

  async function api(method, path, body, retried) {
    var s = stored(SESSION);
    if (s && s.expires_at - 60 < now() && s.refresh_token) { await renew(); s = stored(SESSION); }
    if (!s) throw signedOut();
    var opts = { method: method, headers: { Authorization: "Bearer " + s.access_token } };
    if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
    var r;
    try { r = await fetch(path, opts); } catch (e) { throw Problem(OFFLINE); }
    if (r.status === 401) {
      if (!retried && await renew()) return api(method, path, body, true);
      throw signedOut();
    }
    var data = await r.json().catch(function () { return null; });
    if (!r.ok) throw Problem(apiMessage(r.status, data && data.detail), r.status);
    return data;
  }

  function go(hash) { if (location.hash === hash) draw(); else location.hash = hash; }
  // Use a button, not a link: this screen can already be at #/home, where a link to #/home would do nothing.
  function toAlbums(primary) {
    return button("Go to your albums", { class: primary ? "btn primary" : "btn", onclick: function () { go("#/home"); } });
  }

  // ------------------------------------------------------------------ screens (each returns its page parts)

  function invitedNotice() {
    return pendingInvite() ? el("div", { class: "notice" },
      "You've been invited to an album. Create an account or sign in to join.") : null;
  }

  function mark() {
    var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    svg.setAttribute("width", "72"); svg.setAttribute("height", "72"); svg.setAttribute("viewBox", "0 0 72 72");
    svg.setAttribute("class", "mark"); svg.setAttribute("aria-hidden", "true");
    [["rect", { width: 72, height: 72, rx: 18, fill: "currentColor" }],
     ["circle", { cx: 36, cy: 36, r: 17, fill: "none", stroke: "var(--on-accent)", "stroke-width": 4 }],
     ["circle", { cx: 36, cy: 36, r: 6, fill: "var(--on-accent)" }]].forEach(function (shape) {
      var n = document.createElementNS(NS, shape[0]);
      Object.keys(shape[1]).forEach(function (k) { n.setAttribute(k, shape[1][k]); });
      svg.append(n);
    });
    return svg;
  }

  function emailField() {
    return field("email", "Email", { type: "email", autocomplete: "email", placeholder: "you@example.com" });
  }

  function need(value, message) { if (!value) throw Problem(message); return value; }

  function signIn() {
    var email = emailField();
    var password = field("password", "Password",
                         { type: "password", autocomplete: "current-password", placeholder: "Your password" });
    return [
      el("div", { class: "logo" }, mark(), el("h1", {}, "Memoria"),
         el("div", { class: "muted" }, "Photos, shared with the people you love.")),
      invitedNotice(),
      form([email, password], "Sign in", async function () {
        var e = need(email.input.value.trim(), "Type your email.");
        var p = need(password.input.value, "Type your password.");
        adopt(await authCall("POST", "token?grant_type=password", { email: e, password: p }));
        go("#/home");
      }, el("a", { class: "link middle", href: "#/forgot" }, "Forgot your password?")),
      el("div", { class: "bottom center" }, el("div", { class: "muted" }, "New here?"),
         el("a", { class: "btn", href: "#/signup" }, "Create an account"))
    ];
  }

  function signUp() {
    var name = field("name", "What's your name?", { autocomplete: "name", maxlength: 100, placeholder: "For example: Maya" });
    var email = emailField();
    var password = field("password", "Choose a password",
                         { type: "password", autocomplete: "new-password", placeholder: "At least 8 characters" },
                         "Your phone can remember it for you.");
    return [
      back("#/signin"), el("h1", {}, "Create your account"), invitedNotice(),
      form([name, email, password], "Continue", async function () {
        var n = need(name.input.value.trim(), "Type your name.");
        var e = need(email.input.value.trim(), "Type your email.");
        if (password.input.value.length < 8) throw Problem("Use at least 8 characters for your password.");
        var r = await authCall("POST", "signup?redirect_to=" + encodeURIComponent(location.origin + "/app"),
                               { email: e, password: password.input.value, data: { name: n } });
        if (r.access_token) { adopt(r); go("#/home"); return; }
        // With email confirmation on, an existing address comes back as a user without any identities.
        var user = r.user || r;
        if (user.identities && !user.identities.length) throw Problem("You already have an account. Sign in instead.");
        go("#/checkemail");
      }, el("a", { class: "link middle", href: "#/signin" }, "I already have an account"))
    ];
  }

  function checkEmail() {
    return [back("#/signin"), el("h1", {}, "Check your email"),
            el("p", {}, "We sent you a link. Open it on this device to finish.")];
  }

  function forgot() {
    var email = emailField(), status = el("p", { class: "status", role: "status" });
    return [
      back("#/signin"), el("h1", {}, "Forgot your password?"),
      el("p", { class: "muted" }, "We'll email you a link to choose a new one."),
      form([email], "Send the link", async function () {
        var e = need(email.input.value.trim(), "Type your email.");
        status.textContent = "";
        await authCall("POST", "recover?redirect_to=" + encodeURIComponent(location.origin + "/app"), { email: e });
        status.textContent = "If that email has an account, the link is on its way.";
      }, status)
    ];
  }

  function newPassword() {
    var password = field("password", "New password",
                         { type: "password", autocomplete: "new-password", placeholder: "At least 8 characters" });
    return [
      el("h1", {}, "Choose a new password"),
      form([password], "Save password", async function () {
        if (password.input.value.length < 8) throw Problem("Use at least 8 characters for your password.");
        await api("GET", "/api/me");  // renews the sign-in if needed
        await authCall("PUT", "user", { password: password.input.value }, stored(SESSION).access_token);
        flash = "Your password is saved.";
        go("#/home");
      })
    ];
  }

  async function home(first) {
    var albums = await api("GET", "/api/libraries/overview");
    if (first && albums.length === 1 && !flash) { go("#/album/" + albums[0].id); return null; }
    filters = {};
    return [
      el("div", { class: "bar" }, el("h1", {}, "Your albums"), iconLink("#/settings", "gear", "Settings")),
      albums.length ? el("div", { class: "albums" }, albums.map(function (a) {
        return el("a", { class: "album", href: "#/album/" + a.id },
          el("div", { class: "cover" }, a.cover_url ? el("img", { src: a.cover_url, alt: "" }) : icon("photo", 48)),
          el("strong", {}, a.name), el("span", {}, photoCount(a.photo_count)));
      })) : el("p", { class: "muted" }, "You don't have an album yet. Create one, or join one you were invited to."),
      el("div", { class: "bottom" },
         el("a", { class: "btn primary", href: "#/create" }, icon("plus"), "Create an album"),
         el("a", { class: "btn", href: "#/join" }, "Join with a code"))
    ];
  }

  function create() {
    var name = field("album", "Album name", { autocomplete: "off", maxlength: 100, placeholder: "For example: Rivera family" },
                     "You can change it later.");
    return [
      back("#/home"), el("h1", {}, "Create an album"),
      el("p", { class: "muted" }, "A place to share photos with the people you invite."),
      form([name], "Create album", async function () {
        var n = need(name.input.value.trim(), "Give your album a name.");
        var made = await api("POST", "/api/libraries", { name: n });
        go("#/invite/" + made.id + "/new");
      })
    ];
  }

  function joinScreen() {
    var code = field("code", "Album code", { autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
                                             maxlength: 20, placeholder: "K7M-4PX" },
                     "Capital or small letters both work.");
    code.input.className = "input code";
    return [
      back("#/home"), el("h1", {}, "Join an album"),
      el("p", { class: "muted" }, "Type the code someone sent you. It looks like K7M-4PX."),
      form([code], "Join album", async function () {
        var c = need(code.input.value.trim(), "Type the code.");
        joined(await api("POST", "/api/invites/join", { code: c }));
      }),
      el("div", { class: "notice small" }, "Got a link instead? Just tap it. You don't need to type anything.")
    ];
  }

  function joined(result) {
    flash = (result.already_member ? "You're already in " : "You joined ") + result.name + ".";
    go("#/album/" + result.library_id);
  }

  // An invite link opened this page: join straight away, there is nothing to type.
  async function useInvite() {
    try {
      var result = await api("POST", "/api/invites/join", { token: pendingInvite() });
      localStorage.removeItem(INVITE);
      joined(result);
      return null;
    } catch (problem) {
      if (problem.status !== 400) throw problem;  // offline and the like: keep the invite and let them try again
      localStorage.removeItem(INVITE);
      return [el("h1", {}, "Join an album"), el("p", { class: "error", role: "alert" }, problem.friendly),
              el("div", { class: "bottom" }, toAlbums(true))];
    }
  }

  async function findAlbum(id) {
    var albums = await api("GET", "/api/libraries/overview");
    var found = albums.filter(function (a) { return a.id === id; })[0];
    if (!found) throw Problem("This album isn't available to you any more.", 404);
    return found;
  }

  function today() {
    var d = new Date();
    return ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2);
  }

  async function album(id) {
    var chosen = filterQuery(filters[id] || noFilters(), today());
    var items = [], cursor = null, a = null;
    var photos = el("div", {}), note = el("p", { class: "muted small" });
    var error = el("p", { class: "error", role: "alert" });
    var more = button("Show more photos", { hidden: true });

    async function load() {
      var page = await api("GET", "/api/photos?limit=60&library_id=" + encodeURIComponent(id) +
                                  (chosen ? "&" + chosen : "") +
                                  (cursor ? "&cursor=" + encodeURIComponent(cursor) : ""));
      items = items.concat(page.items);
      cursor = page.next_cursor;
      var shown = items.filter(function (p) { return p.thumbnail_url; });
      var waiting = items.filter(function (p) { return p.status === "processing"; }).length;
      if (!chosen) note.textContent = waiting ? photoCount(waiting) + " still being prepared." : "";
      photos.replaceChildren.apply(photos, byMonth(shown).map(function (g) {
        return el("section", {}, el("h2", {}, g.label), el("div", { class: "grid" }, g.items.map(function (p) {
          return el("div", { class: "tile" },
                    el("img", { src: p.thumbnail_url, alt: p.filename || "Photo", loading: "lazy" }));
        })));
      }));
      if (!items.length) {
        photos.replaceChildren(el("p", { class: "muted" }, chosen ? "No photos match." : "No photos yet."));
      }
      more.hidden = !cursor;
    }
    more.addEventListener("click", async function () {
      more.disabled = true; error.textContent = "";
      try { await load(); } catch (e) { error.textContent = e.friendly || GENERIC; }
      more.disabled = false;
    });

    // Ask for everything at once rather than one thing after another.
    var results = await Promise.all([
      findAlbum(id), load(),
      chosen ? api("GET", "/api/photos/count?library_id=" + encodeURIComponent(id) + "&" + chosen) : null
    ]);
    a = results[0];
    if (results[2]) note.textContent = photoCount(results[2].count) + " found.";
    var canAdd = a.role === "owner" || a.role === "editor";
    return [
      back("#/home", "Albums"),
      el("div", { class: "bar album" }, el("h1", {}, a.name), el("div", { class: "actions" },
         a.role === "owner" ? iconLink("#/invite/" + id, "invite", "Invite people") : null,
         iconLink("#/albumsettings/" + id, "gear", "Album settings"))),
      el("div", { class: "chips" },
         el("a", { class: chosen ? "chip on" : "chip", href: "#/filter/" + id }, icon("filter", 20), "Filter")),
      chosen ? el("p", { class: "summary" }, filterSummary(filters[id], names)) : null,
      chosen ? button("Show all photos", { class: "link", onclick: function () { delete filters[id]; draw(); } }) : null,
      note, photos, error,
      el("div", { class: "bottom" }, more,
         canAdd ? el("a", { class: "btn primary", href: "/upload?library=" + encodeURIComponent(id) },
                     icon("plus"), "Add photos") : null)
    ];
  }

  function facePicture(face) {
    var NS = "http://www.w3.org/2000/svg", svg = document.createElementNS(NS, "svg");
    var box = faceBox(face.bbox, face.width, face.height);
    svg.setAttribute("class", "face");
    svg.setAttribute("aria-hidden", "true");
    if (!face.thumbnail_url) return svg;
    var image = document.createElementNS(NS, "image");
    image.setAttribute("href", face.thumbnail_url);
    if (box) {
      svg.setAttribute("viewBox", [box.x, box.y, box.side, box.side].join(" "));
      image.setAttribute("width", face.width);
      image.setAttribute("height", face.height);
      image.setAttribute("preserveAspectRatio", "none");
    } else {
      image.setAttribute("width", "100%");
      image.setAttribute("height", "100%");
      image.setAttribute("preserveAspectRatio", "xMidYMid slice");
    }
    svg.append(image);
    return svg;
  }

  async function filterScreen(id) {
    var both = await Promise.all([findAlbum(id),
                                  api("GET", "/api/photos/filters?library_id=" + encodeURIComponent(id))]);
    var a = both[0], choices = both[1];
    var draft = JSON.parse(JSON.stringify(filters[id] || noFilters()));
    var toggles = [], timer = null, asked = 0, allShown = false, FIRST = 8;
    var show = button("Show photos", { class: "btn primary", onclick: function () {
      filters[id] = draft;
      go("#/album/" + id);
    } });
    var error = el("p", { class: "error", role: "alert" });

    // Every on/off button registers how to tell whether it is on, so one refresh keeps them all honest.
    function toggle(props, content, isOn, change) {
      var b = el("button", Object.assign({ type: "button" }, props), content);
      b.addEventListener("click", function () { change(); refresh(); recount(); });
      toggles.push({ node: b, isOn: isOn });
      return b;
    }
    function flip(list, value) {
      var at = list.indexOf(value);
      if (at >= 0) list.splice(at, 1); else list.push(value);
    }

    // People
    var people = choices.people.map(function (p, i) {
      var label = p.display_name + (p.qualifier ? ", " + p.qualifier : "");
      names[p.id] = p.display_name;
      return toggle({ class: "person" }, [facePicture(p.face), el("span", {}, label)],
                    function () { return draft.people.indexOf(p.id) >= 0; },
                    function () { flip(draft.people, p.id); });
    });
    var seeAll = button("See all " + choices.people.length + " people", { class: "link middle", onclick: function () {
      allShown = !allShown; refresh();
    } });
    var matchHint = el("p", { class: "hint" });
    var match = el("div", { class: "segment", role: "group", "aria-label": "How to match several people" },
      [["all", "Together"], ["any", "Any of them"]].map(function (m) {
        return toggle({}, m[1], function () { return draft.match === m[0]; }, function () { draft.match = m[0]; });
      }));

    // When
    function when(kind, label, year) {
      return toggle({ class: "chip" }, label,
                    function () { return draft.when.kind === kind && (kind !== "year" || draft.when.year === year); },
                    function () { draft.when = { kind: kind, year: year }; });
    }
    function day(key, label) {
      var f = field("date-" + key, label, { type: "date", value: draft.when[key] || null });
      f.input.addEventListener("change", function () { draft.when[key] = f.input.value; recount(); });
      return f;
    }
    var from = day("from", "From"), to = day("to", "To");
    var range = el("div", { class: "stack" }, from.node, to.node);

    // Added by
    var uploaders = choices.uploaders.map(function (u) {
      names[u.id] = u.is_me ? "me" : u.name;
      return toggle({ class: "chip" }, u.is_me ? "Me" : u.name,
                    function () { return draft.uploaders.indexOf(u.id) >= 0; },
                    function () { flip(draft.uploaders, u.id); });
    });

    function refresh() {
      toggles.forEach(function (t) { t.node.setAttribute("aria-pressed", t.isOn() ? "true" : "false"); });
      people.forEach(function (node, i) {  // anyone chosen stays visible
        node.hidden = !allShown && i >= FIRST && node.getAttribute("aria-pressed") !== "true";
      });
      seeAll.hidden = people.length <= FIRST;
      seeAll.textContent = allShown ? "Show fewer people" : "See all " + people.length + " people";
      var several = draft.people.length > 1;
      match.hidden = matchHint.hidden = !several;
      matchHint.textContent = draft.match === "all" ?
        "Only photos with everyone you picked in the same picture." : "Photos with at least one of the people you picked.";
      range.hidden = draft.when.kind !== "range";
    }

    // Say how many photos match before the filters are applied.
    function recount() {
      var mine = ++asked, query = filterQuery(draft, today());
      clearTimeout(timer);
      show.textContent = "Counting…";
      error.textContent = "";
      timer = setTimeout(async function () {
        try {
          var r = await api("GET", "/api/photos/count?library_id=" + encodeURIComponent(id) +
                                   (query ? "&" + query : ""));
          if (mine !== asked) return;  // the choices changed while we were asking
          show.textContent = !r.count ? "No photos match" : query ? "Show " + photoCount(r.count) : "Show all photos";
          show.disabled = !r.count && !!query;
        } catch (e) {
          if (mine !== asked) return;
          show.textContent = "Show photos";
          show.disabled = false;
          error.textContent = e.friendly || GENERIC;
        }
      }, 200);
    }

    refresh();
    recount();
    var canName = a.role !== "viewer";
    return [
      back("#/album/" + id, "Cancel"),
      el("div", { class: "bar" }, el("h1", {}, "Filter photos"),
         button("Clear all", { class: "link", onclick: function () {
           draft = noFilters(); from.input.value = to.input.value = ""; refresh(); recount();
         } })),
      el("h2", {}, "People"),
      people.length ? el("div", { class: "people" }, people) :
        el("p", { class: "muted" }, "Nobody has been named yet. ",
           canName ? el("a", { href: "/people" }, "Name people") : null),
      seeAll, match, matchHint,
      el("h2", {}, "When"),
      el("div", { class: "chips wrap" }, when("any", "Any time"),
         choices.years.map(function (y) { return when("year", String(y.year), y.year); }),
         when("range", "Pick dates…"), when("today", "On this day"),
         choices.undated ? when("undated", "No date (" + choices.undated + ")") : null),
      range,
      uploaders.length ? [el("h2", {}, "Added by"), el("div", { class: "chips wrap" },
        toggle({ class: "chip" }, "Everyone", function () { return !draft.uploaders.length; },
               function () { draft.uploaders = []; }), uploaders)] : null,
      el("div", { class: "bottom stick" }, error, show)
    ];
  }

  async function invite(id, isNew) {
    var path = "/api/libraries/" + id + "/join-link";
    var both = await Promise.all([findAlbum(id), api("PUT", path, {})]);  // the server allows owners only
    var a = both[0], link = both[1];
    var code = el("span", { class: "bigcode" }), until = el("div", { class: "muted small" });
    var status = el("p", { class: "status", role: "status" }), error = el("p", { class: "error", role: "alert" });
    var manual = el("input", { class: "input", readonly: true, hidden: true, "aria-label": "Invite link" });

    function show() {
      code.textContent = link.code;
      manual.value = link.link;
      until.textContent = "The link and code work until " +
        new Date(link.expires_at).toLocaleDateString("en-US", { month: "long", day: "numeric" }) + ".";
    }
    async function attempt(work) {
      status.textContent = ""; error.textContent = "";
      try { await work(); } catch (e) { error.textContent = e.friendly || GENERIC; }
    }
    async function send() {
      status.textContent = "";
      if (navigator.share) {
        try {
          await navigator.share({ title: "Memoria", text: "Join the album " + a.name + " on Memoria", url: link.link });
          return;
        } catch (e) { if (e.name === "AbortError") return; }
      }
      try { await navigator.clipboard.writeText(link.link); status.textContent = "Link copied. Paste it into your group chat."; }
      catch (e) { manual.hidden = false; manual.select(); status.textContent = "Copy the link below and send it."; }
    }
    function choice(role, title, hint) {
      var radio = el("input", { type: "radio", name: "role", value: role, checked: link.role === role });
      radio.addEventListener("change", function () {
        attempt(async function () {
          try { link = await api("PUT", path, { role: role }); show(); }
          catch (e) {  // show what is really saved
            root.querySelectorAll("input[name=role]").forEach(function (r) { r.checked = r.value === link.role; });
            throw e;
          }
        });
      });
      return el("label", { class: "choice" }, radio, el("div", {}, title, el("span", {}, hint)));
    }
    show();
    return [
      back("#/album/" + id), el("h1", {}, "Invite people"), el("p", { class: "muted" }, "to ", el("strong", {}, a.name)),
      el("div", { class: "stack" },
         button([icon("invite"), "Send invite link"], { class: "btn primary", onclick: send }),
         el("div", { class: "muted small center" }, "Send it in your group chat. Anyone who taps it can join."),
         status, manual,
         el("div", { class: "box center" },
            el("div", { class: "muted small" }, "If the link doesn't work, share this code"), code)),
      el("fieldset", {}, el("legend", {}, "People who join can"),
         choice("editor", "Add and organize photos", "Best for people you trust"),
         choice("viewer", "Only look", "Good for guests")),
      error,
      el("div", { class: "bottom center" },
         el("a", { class: "btn", href: "#/album/" + id }, isNew ? "Do this later" : "Done"),
         until,
         el("button", { type: "button", class: "link middle danger", onclick: async function (event) {
           var pressed = event.currentTarget;
           if (!confirm("Reset the link and code? The old ones will stop working.")) return;
           pressed.disabled = true;
           await attempt(async function () {
             link = await api("POST", path + "/reset");
             manual.hidden = true; show(); status.textContent = "Done. Send the new link or code.";
           });
           pressed.disabled = false;
         } }, "Reset link and code"))
    ];
  }

  function row(href, label) { return el("a", { class: "row", href: href }, label, icon("next", 22)); }

  async function albumSettings(id) {
    var a = await findAlbum(id);
    var error = el("p", { class: "error", role: "alert" });
    return [
      back("#/album/" + id), el("h1", {}, "Album settings"), el("p", { class: "muted" }, a.name),
      a.role === "owner" ? el("div", { class: "stack" }, row("#/rename/" + id, "Rename album"),
                              row("#/invite/" + id, "Invite people")) : null,
      el("div", { class: "bottom center" }, error,
         button("Leave album", { class: "btn danger", onclick: async function (event) {
           var pressed = event.currentTarget;
           if (!confirm("Leave " + a.name + "? Photos you added stay in the album.")) return;
           pressed.disabled = true;
           error.textContent = "";
           try {
             var me = await api("GET", "/api/me");
             await api("DELETE", "/api/libraries/" + id + "/members/" + me.id);
             flash = "You left " + a.name + ".";
             go("#/home");
           } catch (e) { error.textContent = e.friendly || GENERIC; pressed.disabled = false; }
         } }),
         el("div", { class: "muted small" }, "Photos you added stay in the album."))
    ];
  }

  async function rename(id) {
    var a = await findAlbum(id);
    var name = field("album", "Album name", { autocomplete: "off", maxlength: 100, value: a.name },
                     "Everyone in the album will see the new name.");
    return [
      back("#/albumsettings/" + id, "Cancel"), el("h1", {}, "Rename album"),
      form([name], "Save name", async function () {
        var n = need(name.input.value.trim(), "Give your album a name.");
        await api("PATCH", "/api/libraries/" + id, { name: n });
        go("#/albumsettings/" + id);
      })
    ];
  }

  async function settings() {
    var me = await api("GET", "/api/me");
    var name = field("name", "Your name", { autocomplete: "name", maxlength: 100, value: me.display_name });
    var status = el("p", { class: "status", role: "status" });
    return [
      back("#/home"), el("h1", {}, "Settings"),
      form([name], "Save name", async function () {
        var n = need(name.input.value.trim(), "Type your name.");
        status.textContent = "";
        await api("PATCH", "/api/me", { display_name: n });
        status.textContent = "Saved.";
      }, status),
      el("div", { class: "box stack" }, el("div", {}, el("div", { class: "muted small" }, "Email"), me.email || "")),
      el("div", { class: "bottom" }, button("Sign out", { onclick: async function () {
        var s = stored(SESSION);
        localStorage.removeItem(SESSION);
        localStorage.removeItem(INVITE);
        go("#/signin");
        if (s) { try { await authCall("POST", "logout?scope=local", null, s.access_token); } catch (e) { /* already out here */ } }
      } }))
    ];
  }

  // ------------------------------------------------------------------ choosing the screen

  var PUBLIC = { signin: signIn, signup: signUp, forgot: forgot, checkemail: checkEmail };

  async function draw() {
    var mine = ++drawing, first = firstDraw;
    firstDraw = false;
    var parts = location.hash.replace(/^#\/?/, "").split("/");
    var name = parts[0], id = parts[1];
    root.classList.add("busy");
    if (!root.firstChild) root.append(el("p", { class: "muted center" }, "Loading…"));
    try {
      var signedIn = !!stored(SESSION), page;
      if (!signedIn && !PUBLIC[name]) { go(pendingInvite() ? "#/signup" : "#/signin"); return; }
      if (signedIn && PUBLIC[name]) { go("#/home"); return; }
      if (signedIn && pendingInvite() && name !== "newpassword") page = await useInvite();
      else if (PUBLIC[name]) page = PUBLIC[name]();
      else if (name === "newpassword") page = newPassword();
      else if (name === "create") page = create();
      else if (name === "join") page = joinScreen();
      else if (name === "settings") page = await settings();
      else if (name === "album" && id) page = await album(id);
      else if (name === "filter" && id) page = await filterScreen(id);
      else if (name === "invite" && id) page = await invite(id, parts[2] === "new");
      else if (name === "albumsettings" && id) page = await albumSettings(id);
      else if (name === "rename" && id) page = await rename(id);
      else if (name === "home") page = await home(first);
      else { go("#/home"); return; }
      if (mine !== drawing || !page) return;  // a newer screen has taken over
      var banner = flash ? el("div", { class: "notice good", role: "status" }, flash) : null;
      flash = null;
      show([banner].concat(page));
    } catch (problem) {
      if (mine !== drawing || problem.status === 401) return;
      if (!problem.friendly) console.error(problem);
      show([el("h1", {}, "Sorry"), el("p", { class: "error", role: "alert" }, problem.friendly || GENERIC),
            el("div", { class: "bottom" }, button("Try again", { class: "btn primary", onclick: draw }),
               toAlbums(false))]);
    } finally {
      if (mine === drawing) root.classList.remove("busy");
    }
  }

  function show(parts) {
    root.replaceChildren.apply(root, parts.flat(Infinity).filter(Boolean));  // screens may group parts in lists
    var heading = root.querySelector("h1");
    document.title = heading && heading.textContent !== "Memoria" ? heading.textContent + " · Memoria" : "Memoria";
    window.scrollTo(0, 0);
  }

  function start() {
    var entry = parseEntry(location.pathname, location.hash);
    var route = entry.route ? "#" + entry.route : "";
    if (entry.invite) localStorage.setItem(INVITE, JSON.stringify({ token: entry.invite, at: now() }));
    if (entry.auth) { adopt(entry.auth); route = entry.type === "recovery" ? "#/newpassword" : "#/home"; }
    // Take tokens out of the address bar and browser history.
    history.replaceState(null, "", "/app" + (route || (stored(SESSION) ? "#/home" : pendingInvite() ? "#/signup" : "#/signin")));
    window.addEventListener("hashchange", draw);
    draw().then(function () {
      var text = entry.badInvite ? "This invite no longer works. Ask for a new link." :
                 entry.error ? "That email link no longer works. Please try again." : null;
      if (text) root.prepend(el("p", { class: "error", role: "alert" }, text));
    });
  }

  start();
})();
