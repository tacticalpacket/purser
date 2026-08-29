/* purser dashboard renderer.
 *
 * Reads the document out of the non-executable <script type="application/json">
 * block with textContent, parses it, and builds the page from it.
 *
 * THE ONE RULE THIS FILE MUST NOT BREAK: every value out of the document reaches
 * the DOM through textContent or a created text node. There is no innerHTML,
 * no insertAdjacentHTML, no outerHTML and no `new Function` anywhere below, and
 * there must never be one. Merchant descriptions are institution-supplied text
 * that nobody sanitised; a group name is as likely to contain "</script>" or
 * "<img onerror=...>" as an ampersand, and both must land on screen as the
 * literal characters someone's bank actually wrote.
 *
 * Sign convention, stated once here and once on the page: the card is a
 * LIABILITY and the document carries it negative. Everywhere a card figure is
 * displayed on its own it is shown as the amount OWED -- a positive number,
 * labelled "owed". Everywhere a card figure enters arithmetic (the tracked
 * position) it stays negative. The two are never mixed.
 */

(function () {
  "use strict";

  // ---------------------------------------------------------------- helpers

  var GROUP = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });

  function isNum(v) {
    return typeof v === "number" && isFinite(v);
  }

  function money(v) {
    if (!isNum(v)) return "—";
    var s = "$" + GROUP.format(Math.abs(v));
    return v < 0 ? "-" + s : s;
  }

  function moneyAbs(v) {
    return isNum(v) ? "$" + GROUP.format(Math.abs(v)) : "—";
  }

  function compact(v) {
    var a = Math.abs(v);
    if (a >= 1000) return "$" + (v / 1000).toFixed(a >= 10000 ? 0 : 1) + "k";
    return "$" + v.toFixed(0);
  }

  function pct(v) {
    return isNum(v) ? (v * 100).toFixed(1) + "%" : "—";
  }

  /** Create an element. `text` is set with textContent, always. */
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function svgEl(tag, attrs) {
    var node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) {
        node.setAttribute(k, String(attrs[k]));
      }
    }
    return node;
  }

  function svgText(x, y, str, cls, anchor) {
    var t = svgEl("text", { x: x, y: y, "text-anchor": anchor || "middle" });
    if (cls) t.setAttribute("class", cls);
    t.textContent = String(str);
    return t;
  }

  function append(parent) {
    for (var i = 1; i < arguments.length; i++) {
      if (arguments[i]) parent.appendChild(arguments[i]);
    }
    return parent;
  }

  /** A provenance pill. The label set is closed by the contract. */
  function pill(provenance) {
    var known = { direct: 1, institution: 1, derived: 1, inferred: 1, unavailable: 1 };
    var p = String(provenance || "unavailable");
    var node = el("span", "pill pill-" + (known[p] ? p : "unavailable"), p);
    node.title = {
      direct: "Straight from imported institution data. No arithmetic beyond a sum.",
      institution: "The institution's own category or type string, used as given. Not purser's judgement.",
      derived: "Computed by purser from direct facts.",
      inferred: "A heuristic judgement, not a fact. Merchant grouping, recurrence, leak candidacy.",
      unavailable: "The imports cannot establish this.",
    }[p] || "Unrecognised provenance label.";
    return node;
  }

  function tag(text, cls) {
    return el("span", "tag" + (cls ? " " + cls : ""), text);
  }

  function panel(container, title, note) {
    container.textContent = "";
    if (title) container.appendChild(el("h3", "panel-title", title));
    if (note) container.appendChild(el("p", "panel-note", note));
    return container;
  }

  function emptyNote(text) {
    return el("p", "empty", text);
  }

  /** A table. `head` is an array of strings or {t, num}. `rows` is an array of
   *  arrays of strings, numbers, or nodes. Strings become text nodes. */
  function table(head, rows) {
    var wrap = el("div", "tablewrap");
    var t = el("table");
    var thead = el("thead");
    var tr = el("tr");
    head.forEach(function (h) {
      var spec = typeof h === "string" ? { t: h } : h;
      var th = el("th", spec.num ? "num" : null, spec.t);
      tr.appendChild(th);
    });
    thead.appendChild(tr);
    t.appendChild(thead);

    var tbody = el("tbody");
    rows.forEach(function (row) {
      var r = el("tr");
      row.forEach(function (cell, i) {
        var spec = typeof head[i] === "string" ? {} : head[i] || {};
        var td = el("td", spec.num ? "num" : null);
        if (cell && cell.nodeType) td.appendChild(cell);
        else if (cell !== null && cell !== undefined) td.textContent = String(cell);
        else td.textContent = "—";
        r.appendChild(td);
      });
      tbody.appendChild(r);
    });
    t.appendChild(tbody);
    wrap.appendChild(t);
    return wrap;
  }

  function signedCell(v) {
    var span = el("span", isNum(v) && v < 0 ? "neg" : "pos", money(v));
    return span;
  }

  /** Horizontal ranked bar rows. */
  function ranked(items, opts) {
    opts = opts || {};
    var max = 0;
    items.forEach(function (it) {
      max = Math.max(max, Math.abs(it.value || 0));
    });
    var box = el("div", "ranked");
    items.forEach(function (it) {
      var row = el("div", "ranked-row");
      row.appendChild(el("div", "ranked-name", it.name));
      row.appendChild(el("div", "ranked-amount", moneyAbs(it.value)));
      var track = el("div", "ranked-track");
      var fill = el("div", "ranked-fill" + (opts.fill ? " " + opts.fill : ""));
      fill.style.width = (max > 0 ? (Math.abs(it.value) / max) * 100 : 0).toFixed(2) + "%";
      track.appendChild(fill);
      row.appendChild(track);
      if (it.meta || it.metaNodes) {
        var meta = el("div", "ranked-meta", it.meta || "");
        (it.metaNodes || []).forEach(function (n) {
          meta.appendChild(document.createTextNode(" "));
          meta.appendChild(n);
        });
        row.appendChild(meta);
      }
      box.appendChild(row);
    });
    return box;
  }

  // ---------------------------------------------------------------- charts
  //
  // Drawn by hand as inline SVG. No chart library, remote or otherwise: this
  // page must render fully with the network cable pulled.

  function niceTicks(lo, hi) {
    var span = hi - lo;
    if (span <= 0) return [lo, hi];
    var step = Math.pow(10, Math.floor(Math.log10(span / 4)));
    [1, 2, 2.5, 5, 10].some(function (m) {
      if (span / (step * m) <= 5) {
        step = step * m;
        return true;
      }
      return false;
    });
    var ticks = [];
    for (var v = Math.floor(lo / step) * step; v <= hi + step * 0.001; v += step) {
      ticks.push(Math.round(v * 100) / 100);
    }
    return ticks;
  }

  /**
   * Column chart. `labels` are the x categories; `series` is a list of
   * { key, cls, values }. `line` is an optional { cls, values } drawn over it.
   * `dim` is a list of booleans marking columns to render at low opacity
   * (partial months).
   */
  function columnChart(labels, series, line, dim) {
    var W = 900, H = 280;
    var padL = 62, padR = 16, padT = 14, padB = 42;
    var plotW = W - padL - padR, plotH = H - padT - padB;

    var lo = 0, hi = 0;
    series.forEach(function (s) {
      s.values.forEach(function (v) {
        if (isNum(v)) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
      });
    });
    if (line) {
      line.values.forEach(function (v) {
        if (isNum(v)) { hi = Math.max(hi, v); lo = Math.min(lo, v); }
      });
    }
    if (hi === lo) hi = lo + 1;

    var ticks = niceTicks(lo, hi);
    var tLo = ticks[0], tHi = ticks[ticks.length - 1];
    function y(v) { return padT + plotH - ((v - tLo) / (tHi - tLo)) * plotH; }

    var svg = svgEl("svg", {
      class: "chart",
      viewBox: "0 0 " + W + " " + H,
      preserveAspectRatio: "xMidYMid meet",
      role: "img",
    });

    ticks.forEach(function (t) {
      svg.appendChild(svgEl("line", {
        class: "gridline", x1: padL, x2: W - padR, y1: y(t), y2: y(t),
      }));
      svg.appendChild(svgText(padL - 8, y(t) + 4, compact(t), "value", "end"));
    });
    svg.appendChild(svgEl("line", {
      class: "axis", x1: padL, x2: W - padR, y1: y(0), y2: y(0),
    }));

    var n = labels.length || 1;
    var slot = plotW / n;
    var groupW = slot * 0.62;
    var barW = series.length ? groupW / series.length : groupW;

    labels.forEach(function (label, i) {
      var x0 = padL + slot * i + (slot - groupW) / 2;
      var faded = dim && dim[i];
      series.forEach(function (s, j) {
        var v = s.values[i];
        if (!isNum(v)) return;
        var top = Math.min(y(v), y(0)), bot = Math.max(y(v), y(0));
        var rect = svgEl("rect", {
          class: s.cls + (faded ? " partial" : ""),
          x: x0 + barW * j, y: top, width: Math.max(1, barW - 2),
          height: Math.max(1, bot - top), rx: 2,
        });
        var title = svgEl("title", {});
        title.textContent = label + " — " + s.key + " " + money(v);
        rect.appendChild(title);
        svg.appendChild(rect);
      });
      svg.appendChild(svgText(padL + slot * i + slot / 2, H - 22, label));
      if (faded) svg.appendChild(svgText(padL + slot * i + slot / 2, H - 9, "partial"));
    });

    if (line) {
      var pts = [];
      line.values.forEach(function (v, i) {
        if (!isNum(v)) return;
        pts.push([padL + slot * i + slot / 2, y(v)]);
      });
      if (pts.length > 1) {
        svg.appendChild(svgEl("polyline", {
          class: "netline",
          points: pts.map(function (p) { return p[0] + "," + p[1]; }).join(" "),
        }));
      }
      pts.forEach(function (p, i) {
        var c = svgEl("circle", { class: "netdot", cx: p[0], cy: p[1], r: 3.5 });
        var title = svgEl("title", {});
        title.textContent = labels[i] + " — " + line.key + " " + money(line.values[i]);
        c.appendChild(title);
        svg.appendChild(c);
      });
    }
    return svg;
  }

  function legend(entries) {
    var box = el("div", "legend");
    entries.forEach(function (e) {
      var s = el("span");
      s.appendChild(el("span", "swatch " + e.cls));
      s.appendChild(document.createTextNode(e.label));
      box.appendChild(s);
    });
    return box;
  }

  /** A tiny inline sparkline for one category's monthly amounts. */
  function sparkline(values) {
    var W = 120, H = 26;
    var svg = svgEl("svg", { class: "chart", viewBox: "0 0 " + W + " " + H, width: W, height: H });
    var nums = values.filter(isNum);
    if (nums.length < 2) return svg;
    var lo = Math.min.apply(null, nums), hi = Math.max.apply(null, nums);
    if (hi === lo) hi = lo + 1;
    var pts = values.map(function (v, i) {
      var x = (i / (values.length - 1)) * (W - 4) + 2;
      var yy = H - 3 - ((v - lo) / (hi - lo)) * (H - 6);
      return x.toFixed(1) + "," + yy.toFixed(1);
    });
    svg.appendChild(svgEl("polyline", { class: "netline", points: pts.join(" ") }));
    return svg;
  }

  // ---------------------------------------------------------------- sections

  function partialMonths(doc) {
    // The document names the partial edge months outright; complete_months is
    // the fallback when it does not, and a month in neither list is treated as
    // partial. Erring toward "partial" is the safe direction: a month wrongly
    // marked partial understates a trend, a partial month wrongly marked
    // complete lets a half-month drive one.
    var cov = doc.coverage || {};
    if (Array.isArray(cov.partial_months)) {
      var partial = {};
      cov.partial_months.forEach(function (m) { partial[m] = 1; });
      return function (month) { return !!partial[month]; };
    }
    var complete = {};
    (cov.complete_months || []).forEach(function (m) { complete[m] = 1; });
    return function (month) { return !complete[month]; };
  }

  function renderHeader(doc) {
    var cov = doc.coverage || {};
    var line = [];
    if (cov.start && cov.end) line.push(cov.start + " to " + cov.end);
    if (isNum(cov.months)) line.push(cov.months + " months");
    if (isNum(cov.transactions)) line.push(cov.transactions + " transactions");
    document.getElementById("coverage-line").textContent = line.join("  ·  ");
    document.getElementById("generated-line").textContent =
      doc.generated_at ? "document generated " + doc.generated_at : "";
  }

  /** The liability account, if the registry declares one. */
  function liabilityAccount(doc) {
    return (doc.accounts || []).filter(function (a) {
      return a.balance_sign === "liability";
    })[0] || null;
  }

  /** Card figures are displayed as the amount OWED, positive. */
  function owed(amount) {
    if (!isNum(amount)) return { text: "—", qual: "no figure imported", cls: "" };
    if (amount <= 0) return { text: moneyAbs(amount), qual: "owed", cls: "is-liability" };
    return { text: moneyAbs(amount), qual: "in credit — the card owes you", cls: "is-asset" };
  }

  function renderSnapshot(doc) {
    var snap = doc.snapshot || {};
    var liab = liabilityAccount(doc);

    var conv = document.getElementById("sign-convention");
    conv.textContent = "";
    conv.appendChild(el("strong", null, "Sign convention. "));
    conv.appendChild(document.createTextNode(
      "The card is a liability. Wherever a card figure stands on its own it is shown as the " +
      "amount OWED, a positive number labelled “owed”. Wherever a card figure enters " +
      "arithmetic — the tracked position below — it counts against you as a negative. " +
      "Those are the only two treatments on this page and they are never mixed. " +
      "Checking is an asset: positive is money you have."
    ));

    var cards = document.getElementById("snapshot-cards");
    cards.textContent = "";

    function sumcard(kind, label, valueText, valueCls, qual, provenance, note) {
      var c = el("div", "sumcard " + kind);
      c.appendChild(el("div", "sumcard-label", label));
      c.appendChild(el("div", "sumcard-value" + (valueCls ? " " + valueCls : ""), valueText));
      if (qual) c.appendChild(el("div", "sumcard-qual", qual));
      var foot = el("div", "sumcard-foot");
      foot.appendChild(pill(provenance));
      c.appendChild(foot);
      if (note) c.appendChild(el("div", "sumcard-note", note));
      return c;
    }

    var pos = snap.tracked_position || {};
    cards.appendChild(sumcard(
      "is-position", "Tracked-account position",
      money(pos.amount), null,
      pos.as_of ? "as of " + pos.as_of : null,
      pos.provenance || "derived",
      pos.note || "The two tracked accounts only. This is not net worth."
    ));

    var chk = snap.checking || {};
    cards.appendChild(sumcard(
      "is-asset", "Checking",
      money(chk.amount), "is-asset",
      chk.as_of ? "as of " + chk.as_of : null,
      chk.provenance || "direct",
      chk.note || "The institution's own figure at import time, not a live balance."
    ));

    var crd = snap.card || {};
    var o = owed(crd.amount);
    cards.appendChild(sumcard(
      "is-liability", "Credit card",
      o.text, o.cls, (crd.as_of ? o.qual + ", as of " + crd.as_of : o.qual),
      crd.provenance || "direct",
      crd.note || "A liability. Shown as the amount owed; counted negative in the position above."
    ));

    var recent = snap.recent || {};
    var rp = panel(document.getElementById("snapshot-recent"),
      "Last " + (isNum(recent.window_months) ? recent.window_months : "—") + " months",
      "Income is money entering from outside the tracked accounts. Spending counts each real " +
      "purchase once and excludes card payments and internal transfers. These are two separate " +
      "metrics and their difference is not an account balance.");
    if (isNum(recent.income) || isNum(recent.spending)) {
      var g = el("div", "cardgrid");
      g.appendChild(sumcard("is-asset", "Income in", money(recent.income), "is-asset", null,
        recent.provenance || "derived", null));
      g.appendChild(sumcard("", "Spending out", money(recent.spending), null, null,
        recent.provenance || "derived", null));
      g.appendChild(sumcard("is-position", "Net", money(recent.net), null,
        "income minus spending", recent.provenance || "derived", null));
      rp.appendChild(g);
    } else {
      rp.appendChild(emptyNote("No recent window in the document."));
    }

    var ap = panel(document.getElementById("account-table"), "Tracked accounts",
      "Every balance below is the institution's own figure, carried with the date it is as of. " +
      "Purser never invents a “current” balance.");
    var accounts = doc.accounts || [];
    if (!accounts.length) {
      ap.appendChild(emptyNote("No accounts in the document."));
      return;
    }
    ap.appendChild(table(
      ["Account", "Institution", "Type", "Sign", { t: "Latest balance", num: true }, "As of",
       "Balance type", { t: "Available", num: true }, "Provenance"],
      accounts.map(function (a) {
        var lb = a.latest_balance || {};
        var av = a.available_balance;
        var isLiab = a.balance_sign === "liability";
        var shown = isLiab ? owed(lb.amount) : { text: money(lb.amount), qual: "" };
        var amountCell = el("span", null, shown.text);
        if (isLiab && shown.qual) {
          amountCell = el("span");
          amountCell.appendChild(document.createTextNode(shown.text));
          amountCell.appendChild(document.createTextNode(" "));
          amountCell.appendChild(tag(shown.qual, "tag-liability"));
        }
        return [
          a.alias, a.institution, a.type,
          tag(a.balance_sign || "unstated", isLiab ? "tag-liability" : "tag-asset"),
          amountCell, lb.as_of, lb.balance_type,
          av ? money(av.amount) : el("span", "pill pill-unavailable", "unavailable"),
          pill(lb.provenance || "direct"),
        ];
      })
    ));
    if (liab) {
      ap.appendChild(el("p", "panel-note",
        "“" + liab.alias + "” is the liability. Its figure is shown as the amount owed."));
    }
  }

  function renderCashFlow(doc) {
    var rows = doc.cash_flow || [];
    var isPartial = partialMonths(doc);

    var cp = panel(document.getElementById("cashflow-chart"), "Monthly income and spending",
      "Bars are income and spending. The line is net. Months at the edge of coverage are " +
      "partial — shown faded and labelled, and they must not drive a trend claim.");
    if (!rows.length) {
      cp.appendChild(emptyNote("No monthly cash flow in the document."));
    } else {
      cp.appendChild(legend([
        { cls: "swatch-income", label: "income" },
        { cls: "swatch-spending", label: "spending" },
        { cls: "swatch-net", label: "net" },
        { cls: "swatch-partial", label: "partial month" },
      ]));
      cp.appendChild(columnChart(
        rows.map(function (r) { return r.month; }),
        [
          { key: "income", cls: "bar-income", values: rows.map(function (r) { return r.income; }) },
          { key: "spending", cls: "bar-spending", values: rows.map(function (r) { return r.spending; }) },
        ],
        { key: "net", cls: "netline", values: rows.map(function (r) { return r.net; }) },
        rows.map(function (r) { return isPartial(r.month); })
      ));
    }

    var tp = panel(document.getElementById("cashflow-table"), "Month by month",
      "Account movement is the raw per-account sum. It includes transfers and card payments, " +
      "so it is not spending and will not equal income minus spending.");
    if (!rows.length) {
      tp.appendChild(emptyNote("No monthly cash flow in the document."));
    } else {
      tp.appendChild(table(
        ["Month", { t: "Income", num: true }, { t: "Spending", num: true },
         { t: "Net", num: true }, { t: "Account movement", num: true }, "Coverage", "Provenance"],
        rows.map(function (r) {
          return [
            r.month, money(r.income), money(r.spending), signedCell(r.net),
            signedCell(r.account_movement),
            isPartial(r.month) ? tag("partial month", "tag-partial") : tag("complete"),
            pill(r.provenance || "derived"),
          ];
        })
      ));
    }

    var detail = doc.income_detail || [];
    var ip = panel(document.getElementById("income-detail"), "Income detail",
      "Payroll and genuine external deposits only. A savings or certificate drawdown is not " +
      "income, and neither is a transfer between your own accounts — both are excluded here.");
    if (!detail.length) {
      ip.appendChild(emptyNote("No income detail in the document."));
    } else {
      ip.appendChild(table(
        ["Month", "Source", { t: "Amount", num: true }, "Provenance"],
        detail.map(function (d) {
          return [d.month, d.label, money(d.amount), pill(d.provenance || "direct")];
        })
      ));
    }
  }

  function renderSpend(doc) {
    var cats = doc.spend_by_category || [];
    var cp = panel(document.getElementById("category-panel"), "Spending by category",
      "These are the institution's own category strings, used exactly as given. They are not " +
      "purser's judgement, they are not consistent between accounts, and “Other” is " +
      "the institution's catch-all rather than a finding.");
    if (!cats.length) {
      cp.appendChild(emptyNote("No categories in the document."));
    } else {
      cp.appendChild(pill("institution"));
      cp.appendChild(ranked(cats.map(function (c) {
        return {
          name: c.category,
          value: c.amount,
          meta: (isNum(c.n) ? c.n + " transactions" : "") +
                (isNum(c.share) ? "  ·  " + pct(c.share) + " of spending" : ""),
        };
      }), { fill: "is-spend" }));
    }

    var merchants = doc.top_merchants || [];
    var mp = panel(document.getElementById("merchant-panel"), "Top merchant groups",
      "A merchant group is INFERRED: raw descriptions normalised and bucketed by purser. It is " +
      "a guess and it can be wrong. The raw descriptions actually imported are shown under each " +
      "group so you can check the guess.");
    if (!merchants.length) {
      mp.appendChild(emptyNote("No merchant groups in the document."));
    } else {
      mp.appendChild(pill("inferred"));
      var box = el("div", "ranked");
      var max = merchants.reduce(function (m, x) { return Math.max(m, Math.abs(x.amount || 0)); }, 0);
      merchants.forEach(function (m) {
        var row = el("div", "ranked-row");
        row.appendChild(el("div", "ranked-name", m.group));
        row.appendChild(el("div", "ranked-amount", moneyAbs(m.amount)));
        var track = el("div", "ranked-track");
        var fill = el("div", "ranked-fill is-spend");
        fill.style.width = (max > 0 ? (Math.abs(m.amount) / max) * 100 : 0).toFixed(2) + "%";
        track.appendChild(fill);
        row.appendChild(track);

        var meta = el("div", "ranked-meta");
        meta.appendChild(document.createTextNode(
          (isNum(m.n) ? m.n + " charges" : "") +
          (m.first && m.last ? "  ·  " + m.first + " to " + m.last : "") + " "
        ));
        (m.accounts || []).forEach(function (a) {
          meta.appendChild(tag(a));
          meta.appendChild(document.createTextNode(" "));
        });
        (m.raw_examples || []).forEach(function (raw) {
          meta.appendChild(el("span", "raw", raw));
        });
        row.appendChild(meta);
        box.appendChild(row);
      });
      mp.appendChild(box);
    }

    var trend = doc.category_trend || [];
    var tp = panel(document.getElementById("category-trend"), "Category trend",
      "Direction and change across the months the document supplies for each category. " +
      "Partial months are excluded from a trend claim upstream; a direction here is a reading " +
      "of a few months, not a forecast.");
    if (!trend.length) {
      tp.appendChild(emptyNote("No category trend in the document."));
    } else {
      trend.forEach(function (t) {
        var months = t.months || {};
        var keys = Object.keys(months).sort();
        var row = el("div", "trendrow");
        var name = el("div");
        name.appendChild(el("div", "ranked-name", t.category));
        name.appendChild(el("div", "ranked-meta",
          keys.length
            ? keys[0] + " to " + keys[keys.length - 1] + "  ·  " +
              keys.map(function (k) { return moneyAbs(months[k]); }).join("  ")
            : "no months supplied"));
        row.appendChild(name);
        row.appendChild(sparkline(keys.map(function (k) { return months[k]; })));
        var dirCls = { rising: "dir-rising", falling: "dir-falling", flat: "dir-flat" }[t.direction] || "dir-flat";
        var dir = el("div", "trend-dir " + dirCls,
          String(t.direction || "—") +
          (isNum(t.delta_pct) ? "  " + (t.delta_pct > 0 ? "+" : "") + t.delta_pct.toFixed(1) + "%" : ""));
        row.appendChild(dir);
        tp.appendChild(row);
      });
    }

    var largest = doc.largest_transactions || [];
    var lp = panel(document.getElementById("largest-transactions"), "Largest transactions",
      "Individual rows exactly as imported. The description is the institution's raw text and " +
      "the category is the institution's own string.");
    if (!largest.length) {
      lp.appendChild(emptyNote("No transactions in the document."));
    } else {
      lp.appendChild(table(
        ["Date", "Account", "Description", "Category", { t: "Amount", num: true }],
        largest.map(function (t) {
          var desc = el("span", "raw", t.description);
          return [t.date, t.account, desc, t.category, signedCell(t.amount)];
        })
      ));
    }
  }

  function renderCard(doc) {
    var card = doc.card || {};
    var host = document.getElementById("card-panels");
    host.textContent = "";

    var head = el("div", "panel");
    head.appendChild(el("h3", "panel-title", "Card balance"));
    var o = owed(card.balance);
    var grid = el("div", "cardgrid");
    var c = el("div", "sumcard is-liability");
    c.appendChild(el("div", "sumcard-label", "Balance"));
    c.appendChild(el("div", "sumcard-value " + o.cls, o.text));
    c.appendChild(el("div", "sumcard-qual", o.qual));
    var foot = el("div", "sumcard-foot");
    foot.appendChild(pill("direct"));
    c.appendChild(foot);
    c.appendChild(el("div", "sumcard-note",
      "The institution's figure at import time. Not a live balance and not a statement balance."));
    grid.appendChild(c);

    var ic = el("div", "sumcard");
    ic.appendChild(el("div", "sumcard-label", "Interest charged, total"));
    ic.appendChild(el("div", "sumcard-value", money(card.interest_total)));
    ic.appendChild(el("div", "sumcard-qual", "over the covered period"));
    var ifoot = el("div", "sumcard-foot");
    ifoot.appendChild(pill("direct"));
    ic.appendChild(ifoot);
    ic.appendChild(el("div", "sumcard-note",
      "Money paid for carrying a balance, not for anything bought."));
    grid.appendChild(ic);
    head.appendChild(grid);
    host.appendChild(head);

    var up = el("div", "panel");
    up.appendChild(el("h3", "panel-title", "What the imports cannot tell you about this card"));
    up.appendChild(el("p", "panel-note",
      "These are the facts a card view would normally lead with. The exports do not carry them, " +
      "so they are shown as unavailable rather than omitted, estimated or inferred. Do not read " +
      "an absence here as a zero."));
    var unavail = card.unavailable || [];
    if (!unavail.length) {
      up.appendChild(emptyNote("The document lists nothing as unavailable for this card."));
    } else {
      var ul = el("ul", "unavail");
      unavail.forEach(function (u) {
        var li = el("li");
        li.appendChild(el("span", null, u));
        li.appendChild(pill("unavailable"));
        ul.appendChild(li);
      });
      up.appendChild(ul);
    }
    host.appendChild(up);

    var spend = card.monthly_spend || [];
    var sp = el("div", "panel chartpanel");
    sp.appendChild(el("h3", "panel-title", "Card purchases by month"));
    sp.appendChild(el("p", "panel-note",
      "Purchases only. Payments to the card are excluded here — a payment is a transfer " +
      "from checking, not a purchase, and counting it would double-count the spending."));
    if (!spend.length) {
      sp.appendChild(emptyNote("No monthly card spend in the document."));
    } else {
      var isPartial = partialMonths(doc);
      sp.appendChild(legend([
        { cls: "swatch-card", label: "card purchases" },
        { cls: "swatch-partial", label: "partial month" },
      ]));
      sp.appendChild(columnChart(
        spend.map(function (r) { return r.month; }),
        [{ key: "card purchases", cls: "bar-card", values: spend.map(function (r) { return r.amount; }) }],
        null,
        spend.map(function (r) { return isPartial(r.month); })
      ));
    }
    host.appendChild(sp);

    var two = el("div", "twocol");

    var mp = el("div", "panel");
    mp.appendChild(el("h3", "panel-title", "Top merchants on the card"));
    mp.appendChild(el("p", "panel-note", "Merchant groups are inferred, not institution facts."));
    var cm = card.top_merchants || [];
    if (!cm.length) mp.appendChild(emptyNote("No card merchants in the document."));
    else {
      mp.appendChild(pill("inferred"));
      mp.appendChild(ranked(cm.map(function (m) {
        return { name: m.group, value: m.amount, meta: isNum(m.n) ? m.n + " charges" : "" };
      }), { fill: "is-card" }));
    }
    two.appendChild(mp);

    var cp = el("div", "panel");
    cp.appendChild(el("h3", "panel-title", "Top categories on the card"));
    cp.appendChild(el("p", "panel-note", "The institution's own category strings, used as given."));
    var cc = card.top_categories || [];
    if (!cc.length) cp.appendChild(emptyNote("No card categories in the document."));
    else {
      cp.appendChild(pill("institution"));
      cp.appendChild(ranked(cc.map(function (x) {
        return { name: x.category, value: x.amount, meta: isNum(x.n) ? x.n + " transactions" : "" };
      }), { fill: "is-card" }));
    }
    two.appendChild(cp);
    host.appendChild(two);

    var interest = card.interest || [];
    var ip = el("div", "panel");
    ip.appendChild(el("h3", "panel-title", "Interest charged, month by month"));
    if (!interest.length) ip.appendChild(emptyNote("No interest rows in the document."));
    else {
      ip.appendChild(table(
        ["Month", { t: "Interest", num: true }, "Provenance"],
        interest.map(function (r) { return [r.month, money(r.amount), pill("direct")]; })
      ));
      ip.appendChild(el("p", "panel-note",
        "Total " + money(card.interest_total) + ". The APR behind these charges is not in the " +
        "imports and is not shown — see the unavailable list above."));
    }
    host.appendChild(ip);
  }

  function renderRecurring(doc) {
    var rows = doc.recurring || [];
    var p = panel(document.getElementById("recurring-panel"), null, null);
    if (!rows.length) {
      p.appendChild(emptyNote("No recurring charges in the document."));
      return;
    }
    p.appendChild(table(
      ["Charge", "Cadence", { t: "Median", num: true }, { t: "Times", num: true },
       { t: "Distinct months", num: true }, "First", "Last", "Active", "Confidence", "Why"],
      rows.map(function (r) {
        var conf = tag(r.confidence || "unstated",
          r.confidence === "high" ? "tag-asset" : r.confidence === "low" ? "tag-partial" : "");
        var name = el("span", "merchant");
        name.appendChild(document.createTextNode(String(r.group)));
        name.appendChild(document.createTextNode(" "));
        name.appendChild(pill(r.provenance || "inferred"));
        return [
          name, r.cadence, money(r.median_amount), r.occurrences, r.distinct_months,
          r.first, r.last,
          r.still_active === true ? tag("active", "tag-asset")
            : r.still_active === false ? tag("not seen recently", "tag-partial")
            : tag("unstated"),
          conf, r.evidence,
        ];
      })
    ));
    p.appendChild(el("p", "panel-note",
      "Every row here is a heuristic judgement. A high confidence means the pattern is strong, " +
      "not that a subscription exists; only you can say whether a repeated charge is a " +
      "subscription, a habit, or a coincidence of naming."));
  }

  function renderLeaks(doc) {
    var host = document.getElementById("leaks-panel");
    host.textContent = "";
    var rows = doc.leaks || [];
    if (!rows.length) {
      var p = el("div", "panel");
      p.appendChild(emptyNote("No leaks or opportunities in the document."));
      host.appendChild(p);
      return;
    }
    var grid = el("div", "leakgrid");
    rows.forEach(function (leak) {
      var kind = String(leak.classification || "").toUpperCase();
      var cls = kind === "FACT" ? "k-fact"
        : kind === "INFERENCE" ? "k-inference"
        : kind === "POSSIBLE_OPPORTUNITY" ? "k-opportunity" : "";
      var box = el("div", "leak " + cls);
      box.appendChild(el("div", "leak-kind", kind.replace(/_/g, " ") || "UNCLASSIFIED"));
      box.appendChild(el("div", "leak-title", leak.title));
      box.appendChild(el("p", "leak-detail", leak.detail));
      if (isNum(leak.monthly_impact)) {
        box.appendChild(el("div", "leak-impact", money(leak.monthly_impact) + " / month"));
      } else {
        var u = el("div");
        u.appendChild(el("span", "pill pill-unavailable", "no monthly figure"));
        box.appendChild(u);
      }
      if (leak.evidence) box.appendChild(el("div", "leak-evidence", "Evidence: " + leak.evidence));
      grid.appendChild(box);
    });
    host.appendChild(grid);
  }

  function renderQuality(doc) {
    var q = doc.data_quality || {};
    var rec = q.reconciliation || {};

    var rp = panel(document.getElementById("reconciliation-panel"), "Flow reconciliation",
      "Every imported row is classified once. Card payments and internal transfers are excluded " +
      "from spending so a purchase is never counted twice. Anything the rules cannot place with " +
      "confidence is left AMBIGUOUS and folded into neither spending nor income.");
    var keys = ["rows_total", "card_payment", "internal", "income", "spending", "ambiguous"];
    var have = keys.filter(function (k) { return isNum(rec[k]); });
    if (!have.length) {
      rp.appendChild(emptyNote("No reconciliation counts in the document."));
    } else {
      var labels = {
        rows_total: "Rows imported",
        card_payment: "Card payments (excluded from spending)",
        internal: "Internal transfers (excluded from both)",
        income: "Income",
        spending: "Spending",
        ambiguous: "Ambiguous (excluded from both)",
      };
      rp.appendChild(table(
        ["Class", { t: "Rows", num: true }],
        keys.map(function (k) { return [labels[k], isNum(rec[k]) ? rec[k] : "—"]; })
          .concat(isNum(rec.ambiguous_total_amount)
            ? [["Ambiguous rows total", money(rec.ambiguous_total_amount)]] : [])
      ));
      var parts = ["card_payment", "internal", "income", "spending", "ambiguous"];
      var sum = parts.reduce(function (s, k) { return s + (isNum(rec[k]) ? rec[k] : 0); }, 0);
      var total = isNum(rec.rows_total) ? rec.rows_total : null;
      var line = el("p", "panel-note");
      if (total === null) {
        line.textContent = "The document does not state a row total, so the classes cannot be checked against one.";
      } else if (sum === total) {
        line.textContent = "The five classes account for " + sum + " of " + total +
          " imported rows. Every row is placed exactly once.";
      } else {
        line.textContent = "The five classes account for " + sum + " of " + total +
          " imported rows — they do not add up, so some rows are unaccounted for. " +
          "Treat the spending and income figures on this page as incomplete.";
      }
      rp.appendChild(line);
    }

    var np = panel(document.getElementById("quality-notes"), "Notes on these figures", null);
    var notes = q.notes || [];
    if (!notes.length) np.appendChild(emptyNote("No notes in the document."));
    else {
      var ul = el("ul", "notes");
      notes.forEach(function (n) { ul.appendChild(el("li", null, n)); });
      np.appendChild(ul);
    }

    var up = panel(document.getElementById("quality-unavailable"), "Not established by the imports",
      "Shown rather than omitted. An absence here is not a zero and not a nil.");
    var un = q.unavailable || [];
    if (!un.length) up.appendChild(emptyNote("The document lists nothing as unavailable."));
    else {
      var ul2 = el("ul", "unavail");
      un.forEach(function (u) {
        var li = el("li");
        li.appendChild(el("span", null, u));
        li.appendChild(pill("unavailable"));
        ul2.appendChild(li);
      });
      up.appendChild(ul2);
    }
  }

  // ---------------------------------------------------------------- boot

  function fail(message) {
    var box = document.getElementById("load-error");
    box.hidden = false;
    box.textContent = message;
  }

  function main() {
    var block = document.getElementById("purser-document");
    if (!block) return fail("The document block is missing from this page.");

    var doc;
    try {
      doc = JSON.parse(block.textContent);
    } catch (e) {
      return fail("The embedded document is not valid JSON, so nothing below can be trusted: " +
                  e.message);
    }
    if (!doc || typeof doc !== "object") {
      return fail("The embedded document is not an object.");
    }

    renderHeader(doc);
    renderSnapshot(doc);
    renderCashFlow(doc);
    renderSpend(doc);
    renderCard(doc);
    renderRecurring(doc);
    renderLeaks(doc);
    renderQuality(doc);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", main);
  } else {
    main();
  }
})();
