/* Memoria photo viewer: just the photo, with details a swipe up away (docs/UI_SPEC.md, "Photo viewer").
   It lies on top of the album, so closing it returns to the same place with the same filters.
   "Small copy first": the grid's thumbnail shows at once and the sharp copy replaces it when it arrives. Only the
   previous and next sharp copies are fetched ahead of time. */
(function () {
  "use strict";

  var SWIPE = 50;  // pixels a finger must travel to count as a swipe

  // ------------------------------------------------------------------ pure helpers (tested with Node)

  // What a swipe of (dx, dy) pixels means.
  function swipeAction(dx, dy, detailsOpen) {
    if (Math.abs(dx) < SWIPE && Math.abs(dy) < SWIPE) return null;
    if (Math.abs(dx) > Math.abs(dy)) return dx < 0 ? "next" : "previous";
    if (dy < 0) return detailsOpen ? null : "details";
    return detailsOpen ? "hide" : "close";
  }

  // The date as two lines. `local` is the wall-clock time where the photo was taken; it is formatted as written,
  // never converted to this device's time zone. The time of day is only shown when the camera recorded it.
  function dateLines(local, source) {
    var m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(local || "");
    if (!m) return ["Not known", ""];
    var d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]));
    function say(options) { return d.toLocaleString("en-US", Object.assign({ timeZone: "UTC" }, options)); }
    var second = say({ weekday: "long" });
    if (/^exif/.test(source || "")) second += ", " + say({ hour: "numeric", minute: "2-digit" });
    return [say({ month: "long", day: "numeric", year: "numeric" }), second];
  }

  // The day a photo was added to the album, in this device's own time zone.
  function addedDate(instant) {
    var d = new Date(instant);
    return isNaN(d) ? "" : d.toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" });
  }

  // Phones save through the share menu ("Save Image"); computers download a file.
  function savesByShare(touchScreen, canShareFiles) { return !!(touchScreen && canShareFiles); }

  if (typeof module !== "undefined") {
    module.exports = { swipeAction: swipeAction, dateLines: dateLines, savesByShare: savesByShare,
                       addedDate: addedDate };
  }
  if (typeof document === "undefined") return;

  // ------------------------------------------------------------------ the viewer

  var current = null;  // the open viewer, if any

  function canShareFiles() {
    try {
      return !!navigator.canShare && navigator.canShare({ files: [new File(["x"], "x.jpg", { type: "image/jpeg" })] });
    } catch (e) { return false; }
  }

  function loadImage(url) {
    return new Promise(function (done, failed) {
      var image = new Image();
      image.onload = function () { done(image); };
      image.onerror = failed;
      image.src = url;
    });
  }

  /* options: el, icon, api, facePicture (from the app); photos() -> the photos in grid order;
     more() -> a promise that loads the next batch, or null when there is none; startId;
     closed(lastId, datesChanged). */
  function open(options) {
    if (current) return;
    var el = options.el, icon = options.icon;
    var shownId = null, turn = 0, leaving = false, redated = false;
    var sharp = new Map(), details = new Map();
    var byShare = savesByShare(window.matchMedia("(pointer: coarse)").matches, canShareFiles());

    var picture = el("img", { class: "photo", alt: "Photo", draggable: "false" });
    var stage = el("div", { class: "stage" }, picture);
    function control(name, label, glyph, action) {
      return el("button", { type: "button", class: "viewer-btn " + name, "aria-label": label, onclick: action },
                icon(glyph, 28));
    }
    var closeButton = control("close", "Close photo", "cross", close);
    var previous = control("arrow previous", "Previous photo", "back", function () { step(-1); });
    var next = control("arrow next", "Next photo", "next", function () { step(1); });
    var detailsButton = el("button", { type: "button", class: "viewer-btn details-button",
                                       onclick: function () { setDetails(true); } }, icon("up", 24), "Details");
    var problem = el("div", { class: "viewer-problem", role: "status", hidden: true },
                     el("span", {}, "This photo didn't load."),
                     el("button", { type: "button", class: "link", onclick: function () { show(shownId); } }, "Try again"));
    var handle = el("button", { type: "button", class: "handle", "aria-label": "Hide details",
                                onclick: function () { setDetails(false); } }, el("span", {}));
    var body = el("div", { class: "sheet-body" });
    var sheet = el("div", { class: "sheet", hidden: true }, handle, body);
    var box = el("div", { class: "viewer", role: "dialog", "aria-modal": "true", "aria-label": "Photo" },
                 stage, closeButton, previous, next, detailsButton, problem, sheet);

    function find(id) {
      var list = options.photos();
      var at = list.findIndex(function (p) { return p.id === id; });
      return { list: list, at: at, photo: list[at] };
    }

    // The sharp copy of a photo, fetched once. The links expire after a few minutes, so a failure asks for a new one.
    function sharpCopy(photo) {
      if (!sharp.has(photo.id)) {
        var job = (photo.display_url ? loadImage(photo.display_url) : Promise.reject()).catch(async function () {
          var fresh = await options.api("GET", "/api/photos/" + photo.id + "/url?size=display");
          photo.display_url = fresh.url;
          return loadImage(fresh.url);
        });
        sharp.set(photo.id, job);
        job.catch(function () { sharp.delete(photo.id); });
      }
      return sharp.get(photo.id);
    }

    function neighbours() {
      var here = find(shownId);
      return [here.list[here.at - 1], here.list[here.at + 1]];
    }

    function prepare() {
      if (shownId === null) return;
      var around = neighbours(), keep = [shownId].concat(around.filter(Boolean).map(function (p) { return p.id; }));
      Array.from(sharp.keys()).forEach(function (id) { if (keep.indexOf(id) < 0) sharp.delete(id); });
      around.filter(Boolean).forEach(function (p) { sharpCopy(p).catch(function () { /* tried again when shown */ }); });
      previous.disabled = !around[0];
      next.disabled = !around[1];
    }

    // Near the end of what is loaded, fetch the next batch so swiping can carry on.
    async function loadAhead() {
      var here = find(shownId);
      if (here.at < here.list.length - 2) return;
      var pending = options.more();
      if (!pending) return;
      try { await pending; } catch (e) { return; }  // swiping to the end again retries
      if (current === self) { prepare(); loadAhead(); }
    }

    function show(id) {
      var here = find(id), photo = here.photo;
      if (!photo) return;
      var mine = ++turn;
      shownId = id;
      problem.hidden = true;
      var date = dateLines(photo.taken_local, photo.taken_at_source)[0];
      picture.alt = date === "Not known" ? "Photo with no date" : "Photo from " + date;
      picture.dataset.copy = "small";
      picture.src = photo.thumbnail_url;
      var arriving = sharpCopy(photo);
      arriving.then(function (image) {
        if (mine !== turn) return;
        picture.src = image.src;
        picture.dataset.copy = "sharp";
      }, function () { if (mine === turn) problem.hidden = false; });
      prepare();
      loadAhead();
      if (!sheet.hidden) fillDetails();
    }

    function step(by) {
      var here = find(shownId), target = here.list[here.at + by];
      if (target) show(target.id);
    }

    // The button under the date. The copy it saves has no location (the server makes it that way).
    function saveControl(photoId) {
      var label = byShare ? "Save photo" : "Download";
      var go = el("button", { type: "button", class: "btn primary" }, label);
      var status = el("p", { class: "status", role: "status" }), file = null, copyInfo = null;
      function stillHere() { return current === self && photoId === shownId && !sheet.hidden; }
      function say(text, bad) { status.textContent = text; status.className = bad ? "error" : "status"; }

      function download(copy) {
        el("a", { href: copy.url, download: copy.filename, rel: "noopener" }).click();
      }

      async function share() {
        if (!navigator.canShare({ files: [file] })) {  // a kind of picture this phone's share menu will not take
          download(copyInfo);
          go.textContent = label;
          say("Saved to your downloads.");
          return;
        }
        try {
          await navigator.share({ files: [file] });
          go.textContent = label;
          say("");
        } catch (e) {
          if (e.name === "AbortError") return;  // closed the share menu: nothing to report
          if (e.name !== "NotAllowedError") throw e;
          // Preparing took so long that the phone wants a fresh tap before opening its share menu.
          go.textContent = "Tap to save";
          say("Your photo is ready.");
        }
      }

      go.addEventListener("click", async function () {
        go.disabled = true;
        say("");
        try {
          if (!file) {
            go.textContent = "Preparing…";
            var copy = await options.api("POST", "/api/photos/" + photoId + "/save-copy");
            if (!stillHere()) return;
            if (!byShare) {
              download(copy);
              say("Downloading. Look in your Downloads folder.");
              return;
            }
            copyInfo = copy;
            var response = await fetch(copy.url);
            if (!response.ok) throw new Error("download failed: " + response.status);
            file = new File([await response.blob()], copy.filename, { type: copy.content_type });
            if (!stillHere()) return;
          }
          await share();
        } catch (e) {
          file = null;
          say(e.friendly || "Couldn't save the photo. Please try again.", true);
        } finally {
          if (go.textContent === "Preparing…") go.textContent = label;
          go.disabled = false;
        }
      });
      return el("div", { class: "save" }, go, status,
                el("p", { class: "muted small" }, "Saved copies keep the date, and leave out location and camera details."));
    }

    // "Add date" (or "Change date") under the date taken, for the person who added the photo.
    function dateControl(photo) {
      var known = !!photo.taken_local, holder = el("div", { class: "set-date" });
      var start = el("button", { type: "button", class: "link", onclick: edit }, known ? "Change date" : "Add date");
      function stop() { body.classList.remove("editing"); holder.replaceChildren(start); }
      function edit() {
        var now = new Date();
        var today = now.getFullYear() + "-" + ("0" + (now.getMonth() + 1)).slice(-2) + "-" +
                    ("0" + now.getDate()).slice(-2);
        var input = el("input", { type: "date", class: "input", id: "taken-date", min: "1850-01-01", max: today,
                                  value: known ? photo.taken_local.slice(0, 10) : null });
        var problemText = el("p", { class: "error", role: "alert" });
        var save = el("button", { type: "submit", class: "btn primary" }, "Save date");
        var form = el("form", { novalidate: true },
          el("label", { for: "taken-date" }, "When was it taken?"), input, problemText,
          el("div", { class: "pair" }, save,
             el("button", { type: "button", class: "btn", onclick: stop }, "Cancel")));
        input.addEventListener("input", function () { problemText.textContent = ""; });
        form.addEventListener("submit", async function (e) {
          e.preventDefault();
          if (save.disabled) return;
          var day = input.value;
          if (!day || day < "1850-01-01" || day > today) {
            problemText.textContent = "Pick a date between 1850 and today.";
            return;
          }
          save.disabled = true;
          problemText.textContent = "";
          try {
            await options.api("PATCH", "/api/photos/" + photo.id, { taken_at: day + "T12:00:00" });
          } catch (err) {
            problemText.textContent = err.friendly || "Something went wrong. Please try again.";
            save.disabled = false;
            return;
          }
          photo.taken_local = day + "T12:00:00";
          photo.taken_at_source = "manual";
          redated = true;
          if (current === self && photo.id === shownId && !sheet.hidden) fillDetails();
        });
        body.classList.add("editing");  // one thing at a time: "Save photo" steps aside
        holder.replaceChildren(form);
        input.focus();
      }
      holder.append(start);
      return holder;
    }

    async function fillDetails() {
      var id = shownId, photo = find(id).photo, date = dateLines(photo.taken_local, photo.taken_at_source);
      var rest = el("div", {}, el("p", { class: "muted" }, "Loading…"));
      var dating = el("div", {});
      body.classList.remove("editing");
      body.replaceChildren(el("div", { class: "muted small" }, "Date taken"),
        el("div", { class: "when" },
           el("div", {}, el("h2", { class: "date" }, date[0]), date[1] ? el("p", { class: "muted" }, date[1]) : null),
           dating),
        saveControl(id), rest);
      body.scrollTop = 0;
      try {
        if (!details.has(id)) details.set(id, await options.api("GET", "/api/photos/" + id + "/details"));
      } catch (e) {
        details.delete(id);
        if (id === shownId) rest.replaceChildren(el("p", { class: "error" }, e.friendly || "Something went wrong."));
        return;
      }
      if (id !== shownId) return;
      var d = details.get(id);
      if (d.can_set_date) dating.replaceChildren(dateControl(photo));
      rest.replaceChildren(
        el("h3", {}, "In this photo"),
        d.people.length ? el("ul", { class: "faces" }, d.people.map(function (p) {
          return el("li", {}, options.facePicture({ thumbnail_url: photo.display_url, bbox: p.bbox, width: d.width,
                                                    height: d.height }),
                    el("span", {}, p.display_name + (p.qualifier ? ", " + p.qualifier : "")));
        })) : el("p", { class: "muted" }, "Nobody has been named in this photo."),
        el("div", { class: "added" }, el("div", { class: "muted small" }, "Date added"),
           el("strong", {}, addedDate(d.added_at)),
           el("div", { class: "who" }, "by " + (d.added_by.is_me ? "you" : d.added_by.name))));
      sheet.classList.toggle("scrolls", body.scrollHeight > body.clientHeight);
    }

    function setDetails(on) {
      if (sheet.hidden !== on) return;
      sheet.hidden = !on;
      box.classList.toggle("open", on);
      detailsButton.hidden = on;
      if (on) { fillDetails(); handle.focus(); } else detailsButton.focus();
    }

    function act(action) {
      if (action === "next") step(1);
      else if (action === "previous") step(-1);
      else if (action === "details") setDetails(true);
      else if (action === "hide") setDetails(false);
      else if (action === "close") close();
    }

    // One finger (or a mouse) swipes. Two fingers zoom, and while zoomed in a finger moves around the photo.
    function zoomed() { return !!window.visualViewport && window.visualViewport.scale > 1.05; }
    function swipes(area, only) {
      var fingers = new Set(), start = null;
      area.addEventListener("pointerdown", function (e) {
        fingers.add(e.pointerId);
        start = fingers.size === 1 && !zoomed() ? { x: e.clientX, y: e.clientY, id: e.pointerId } : null;
      });
      area.addEventListener("pointerup", function (e) {
        fingers.delete(e.pointerId);
        if (!start || start.id !== e.pointerId) return;
        var action = swipeAction(e.clientX - start.x, e.clientY - start.y, !sheet.hidden);
        start = null;
        if (action && (!only || only.indexOf(action) >= 0)) act(action);
      });
      area.addEventListener("pointercancel", function (e) { fingers.delete(e.pointerId); start = null; });
      area.addEventListener("pointerleave", function (e) { fingers.delete(e.pointerId); });
    }
    swipes(stage);
    swipes(sheet, ["hide", "next", "previous"]);

    // When the details are long enough to scroll, the browser takes over up-and-down drags (and cancels the pointer
    // events). A drag downward that starts at the very top has nothing to scroll, so it still means "hide".
    var pull = null;
    body.addEventListener("touchstart", function (e) {
      pull = sheet.classList.contains("scrolls") && body.scrollTop <= 0 && e.touches.length === 1 ?
        { x: e.touches[0].clientX, y: e.touches[0].clientY } : null;
    }, { passive: true });
    body.addEventListener("touchend", function (e) {
      if (!pull || zoomed()) return;
      var end = e.changedTouches[0];
      if (body.scrollTop <= 0 && swipeAction(end.clientX - pull.x, end.clientY - pull.y, true) === "hide") act("hide");
      pull = null;
    }, { passive: true });

    function onKey(e) {
      if (e.target && e.target.tagName === "INPUT" && e.key !== "Escape") return;  // typing a date
      var keys = { ArrowLeft: "previous", ArrowRight: "next", ArrowUp: "details", ArrowDown: "hide" };
      if (e.key === "Escape") { e.preventDefault(); act(sheet.hidden ? "close" : "hide"); }
      else if (keys[e.key]) { e.preventDefault(); act(keys[e.key]); }
    }
    function onZoom() { box.classList.toggle("zoomed", zoomed()); }

    // The phone's own "back" closes the viewer: opening adds a history step that closing takes away again.
    function close() {
      if (leaving) return;
      leaving = true;
      history.back();
    }
    function destroy() {
      if (current !== self) return;
      current = null;
      turn++;
      window.removeEventListener("popstate", destroy);
      document.removeEventListener("keydown", onKey);
      if (window.visualViewport) window.visualViewport.removeEventListener("resize", onZoom);
      box.remove();
      document.body.style.overflow = "";
      document.getElementById("app").inert = false;
      options.closed(shownId, redated);
    }

    var self = { destroy: destroy };
    current = self;
    history.pushState({ viewer: true }, "", location.href);
    window.addEventListener("popstate", destroy);
    document.addEventListener("keydown", onKey);
    if (window.visualViewport) window.visualViewport.addEventListener("resize", onZoom);
    document.body.style.overflow = "hidden";  // the album behind stays where it is
    document.getElementById("app").inert = true;
    document.body.append(box);
    show(options.startId);
    closeButton.focus();
  }

  // For the app: another screen is taking over (for example, the sign-in ran out).
  function dismiss() { if (current) current.destroy(); }

  window.MemoriaViewer = { open: open, dismiss: dismiss };
})();
