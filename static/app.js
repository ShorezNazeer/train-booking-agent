/**
 * Train Booking Engine Frontend Application
 */

// Session Management: Isolate bookings per user session via X-Session-ID stored in localStorage
const SESSION_STORAGE_KEY = "railengine_session_id";

function getSessionId() {
  let sessionId = localStorage.getItem(SESSION_STORAGE_KEY);
  if (!sessionId) {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      sessionId = crypto.randomUUID();
    } else {
      sessionId = "sess_" + Math.random().toString(36).substring(2, 15) + Date.now().toString(36);
    }
    localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
  }
  return sessionId;
}

// Intercept window.fetch to attach X-Session-ID header to all API requests
const _nativeFetch = window.fetch;
window.fetch = function (resource, init) {
  const options = init ? { ...init } : {};
  const sessionId = getSessionId();

  if (options.headers instanceof Headers) {
    options.headers.set("X-Session-ID", sessionId);
  } else if (Array.isArray(options.headers)) {
    options.headers = [...options.headers, ["X-Session-ID", sessionId]];
  } else {
    options.headers = {
      ...(options.headers || {}),
      "X-Session-ID": sessionId,
    };
  }

  return _nativeFetch.call(this, resource, options);
};

const state = {
  fromStation: "Lucknow",
  toStation: "Delhi",
  travelDate: getTomorrowDate(),
  earliest: "00:00",
  sort: "fastest",
  preferredClasses: "",
  journeys: [],
  activeBookings: [],
  selectedJourney: null,
  selectedClass: null,
  chatHistory: [],
  activeDemo: null,
  isSearching: false,
  isChatting: false,
};

// Utility: Date helpers
function getTomorrowDate() {
  const d = new Date();
  d.setDate(d.getDate() + 1);
  return d.toISOString().split("T")[0];
}

function getOffsetDate(days) {
  const d = new Date();
  d.setDate(d.getDate() + days);
  return d.toISOString().split("T")[0];
}

function formatDateStr(isoStr) {
  if (!isoStr) return "";
  try {
    const parts = isoStr.split("-");
    const d = new Date(parts[0], parts[1] - 1, parts[2]);
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  } catch (e) {
    return isoStr;
  }
}

function formatMinutes(min) {
  const h = Math.floor(min / 60);
  const m = min % 60;
  if (h === 0) return `${m}m`;
  if (m === 0) return `${h}h`;
  return `${h}h ${m}m`;
}

// Helper: Check res.ok and read text before calling JSON.parse so real server errors surface clearly
async function parseJsonResponse(res) {
  const text = await res.text();
  if (!res.ok) {
    let errDetail = text;
    try {
      const errJson = JSON.parse(text);
      if (errJson.detail) {
        errDetail = typeof errJson.detail === "string" ? errJson.detail : JSON.stringify(errJson.detail);
      } else if (errJson.reply) {
        errDetail = errJson.reply;
      } else if (errJson.reason) {
        errDetail = errJson.reason;
      } else if (errJson.message) {
        errDetail = errJson.message;
      }
    } catch (_) {}
    throw new Error(errDetail || `Request failed (${res.status})`);
  }
  try {
    return JSON.parse(text);
  } catch (parseErr) {
    throw new Error(`Invalid JSON response: ${text}`);
  }
}

// Initial Setup
document.addEventListener("DOMContentLoaded", () => {
  setupEventListeners();
  setupAutocomplete("fromStationInput", "fromStationDropdown", (val) => {
    state.fromStation = val;
  });
  setupAutocomplete("toStationInput", "toStationDropdown", (val) => {
    state.toStation = val;
  });

  // Load initial bookings count
  refreshBookings();

  // Run initial search
  executeSearch();
});

function setupEventListeners() {
  // Search Form Submit
  const searchForm = document.getElementById("searchForm");
  if (searchForm) {
    searchForm.addEventListener("submit", (e) => {
      e.preventDefault();
      readFormValues();
      executeSearch();
    });
  }

  // Swap Stations Button
  const swapBtn = document.getElementById("swapStationsBtn");
  if (swapBtn) {
    swapBtn.addEventListener("click", () => {
      const fromEl = document.getElementById("fromStationInput");
      const toEl = document.getElementById("toStationInput");
      const temp = fromEl.value;
      fromEl.value = toEl.value;
      toEl.value = temp;
      state.fromStation = fromEl.value;
      state.toStation = toEl.value;
    });
  }

  // Quick Date Chips
  document.querySelectorAll(".quick-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".quick-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      const offset = parseInt(chip.getAttribute("data-days") || "1", 10);
      const newDate = getOffsetDate(offset);
      document.getElementById("travelDateInput").value = newDate;
      state.travelDate = newDate;
    });
  });

  // Class Filter Chips
  document.querySelectorAll(".class-filter-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      document.querySelectorAll(".class-filter-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      state.preferredClasses = chip.getAttribute("data-classes") || "";
      executeSearch();
    });
  });

  // Demo Buttons
  document.getElementById("demoDirectBtn")?.addEventListener("click", () => triggerDemo("direct"));
  document.getElementById("demoSplitBtn")?.addEventListener("click", () => triggerDemo("split"));
  document.getElementById("demoSoldoutBtn")?.addEventListener("click", () => triggerDemo("soldout"));
  document.getElementById("resetDemoBtn")?.addEventListener("click", resetDemos);

  // My Bookings Nav Button
  document.getElementById("myBookingsBtn")?.addEventListener("click", () => {
    openBookingsModal();
  });

  // Chat Input
  const chatForm = document.getElementById("chatForm");
  if (chatForm) {
    chatForm.addEventListener("submit", (e) => {
      e.preventDefault();
      handleChatSubmit();
    });
  }

  // Chat Suggestions
  document.querySelectorAll(".suggestion-chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const text = chip.getAttribute("data-prompt");
      if (text) {
        document.getElementById("chatInput").value = text;
        handleChatSubmit();
      }
    });
  });
}

function readFormValues() {
  state.fromStation = document.getElementById("fromStationInput").value.trim() || "Lucknow";
  state.toStation = document.getElementById("toStationInput").value.trim() || "Delhi";
  state.travelDate = document.getElementById("travelDateInput").value || getTomorrowDate();
  state.earliest = document.getElementById("earliestInput").value || "00:00";
  state.sort = document.getElementById("sortInput").value || "fastest";
}

// ---------- Autocomplete Implementation ----------
function setupAutocomplete(inputId, dropdownId, onSelect) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  if (!input || !dropdown) return;

  let debounceTimer = null;

  input.addEventListener("input", () => {
    clearTimeout(debounceTimer);
    const query = input.value.trim();
    debounceTimer = setTimeout(async () => {
      try {
        const res = await fetch(`/api/stations?q=${encodeURIComponent(query)}`);
        const stations = await parseJsonResponse(res);
        renderDropdown(dropdown, stations, (selected) => {
          input.value = selected.query_val || selected.code;
          onSelect(input.value);
          dropdown.classList.remove("show");
        });
      } catch (err) {
        console.error("Autocomplete fetch error:", err);
      }
    }, 180);
  });

  input.addEventListener("focus", async () => {
    if (!dropdown.classList.contains("show")) {
      try {
        const res = await fetch(`/api/stations?q=${encodeURIComponent(input.value.trim())}`);
        const stations = await parseJsonResponse(res);
        renderDropdown(dropdown, stations, (selected) => {
          input.value = selected.query_val || selected.code;
          onSelect(input.value);
          dropdown.classList.remove("show");
        });
      } catch (err) {
        console.error("Autocomplete fetch error:", err);
      }
    }
  });

  document.addEventListener("click", (e) => {
    if (!input.contains(e.target) && !dropdown.contains(e.target)) {
      dropdown.classList.remove("show");
    }
  });
}

function renderDropdown(dropdown, items, onSelect) {
  if (!items || items.length === 0) {
    dropdown.innerHTML = `<div class="autocomplete-item"><span class="station-match-name">No matching station</span></div>`;
    dropdown.classList.add("show");
    return;
  }

  dropdown.innerHTML = items
    .map(
      (item) => `
    <div class="autocomplete-item" data-code="${item.code}" data-val="${item.query_val || item.code}">
      <span class="station-match-name">${item.name}</span>
      <span class="station-match-code">${item.code}</span>
    </div>
  `
    )
    .join("");

  dropdown.classList.add("show");

  dropdown.querySelectorAll(".autocomplete-item").forEach((el, index) => {
    el.addEventListener("click", () => {
      onSelect(items[index]);
    });
  });
}

// ---------- One-Click Demos ----------
async function triggerDemo(type) {
  try {
    showToast("info", `Activating ${type.toUpperCase()} demo scenario...`);
    const res = await fetch(`/api/demo/${type}`, { method: "POST" });
    const data = await parseJsonResponse(res);
    if (data.success) {
      document.getElementById("fromStationInput").value = data.from_station;
      document.getElementById("toStationInput").value = data.to_station;
      document.getElementById("travelDateInput").value = data.travel_date;
      state.fromStation = data.from_station;
      state.toStation = data.to_station;
      state.travelDate = data.travel_date;
      state.activeDemo = type;

      // Reset filter chips to All Classes so scenario is not blocked by a leftover filter
      document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
      document.querySelector('.chip[data-classes=""]')?.classList.add("active");
      state.preferredClasses = "";

      // Update active demo card style
      document.querySelectorAll(".demo-btn-card").forEach((card) => card.classList.remove("active"));
      if (type === "direct") document.getElementById("demoDirectBtn")?.classList.add("active");
      if (type === "split") document.getElementById("demoSplitBtn")?.classList.add("active");
      if (type === "soldout") document.getElementById("demoSoldoutBtn")?.classList.add("active");

      showToast("success", `Demo loaded: ${data.description}`);
      await executeSearch();
    }
  } catch (err) {
    showToast("warning", `Demo trigger error: ${err.message}`);
  }
}

async function resetDemos() {
  try {
    const res = await fetch("/api/demo/reset", { method: "POST" });
    await parseJsonResponse(res);
    document.querySelectorAll(".demo-btn-card").forEach((card) => card.classList.remove("active"));
    document.querySelectorAll(".chip").forEach((c) => c.classList.remove("active"));
    document.querySelector('.chip[data-classes=""]')?.classList.add("active");
    state.preferredClasses = "";
    state.activeDemo = null;
    showToast("info", "Demo simulation overrides cleared.");
    await executeSearch();
  } catch (err) {
    showToast("warning", `Reset error: ${err.message}`);
  }
}

function showToast(type, message) {
  const banner = document.getElementById("statusToastBanner");
  if (!banner) return;
  banner.className = `alert-banner ${type}`;
  banner.innerHTML = `
    <span>${message}</span>
    <button class="btn-icon" style="background:transparent;border:none;color:inherit;cursor:pointer;" onclick="this.parentElement.style.display='none'">✕</button>
  `;
  banner.style.display = "flex";
}

// ---------- Search Execution ----------
async function executeSearch() {
  const container = document.getElementById("journeysContainer");
  const countEl = document.getElementById("resultsCount");
  const searchBtn = document.getElementById("searchSubmitBtn");

  if (!container) return;

  state.isSearching = true;
  if (searchBtn) {
    searchBtn.disabled = true;
    searchBtn.innerHTML = `<span class="spinner"></span> Searching...`;
  }

  container.innerHTML = `
    <div class="empty-state">
      <div class="spinner" style="width: 32px; height: 32px;"></div>
      <p>Exploring route network and checking live seat availability...</p>
    </div>
  `;

  try {
    const params = new URLSearchParams({
      from_station: state.fromStation,
      to_station: state.toStation,
      travel_date: state.travelDate,
      earliest: state.earliest,
      sort: state.sort,
      limit: "10",
    });
    if (state.preferredClasses) {
      params.append("preferred_classes", state.preferredClasses);
    }

    const res = await fetch(`/api/search?${params.toString()}`);
    const data = await parseJsonResponse(res);

    state.isSearching = false;
    if (searchBtn) {
      searchBtn.disabled = false;
      searchBtn.innerHTML = `🔍 Search Trains`;
    }

    if (!data.success || !data.journeys || data.journeys.length === 0) {
      if (countEl) countEl.innerText = "0 Options Found";
      container.innerHTML = `
        <div class="empty-state">
          <div class="empty-icon">🚆</div>
          <h3>No Routes Found</h3>
          <p>${data.reason || "No journeys with seats were found for the selected stations and date."}</p>
          <button class="btn btn-secondary" onclick="triggerDemo('direct')">Try Direct Train Demo</button>
        </div>
      `;
      return;
    }

    state.journeys = data.journeys;
    const kind = data.journeys[0].transfers === 0 ? "Direct Trains" : "Routes with Transfers";
    if (countEl) {
      countEl.innerHTML = `<span>${data.journeys.length} ${kind} Found</span>`;
    }

    renderJourneys(container, data.journeys);
  } catch (err) {
    state.isSearching = false;
    if (searchBtn) {
      searchBtn.disabled = false;
      searchBtn.innerHTML = `🔍 Search Trains`;
    }
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">⚠️</div>
        <h3>Search Error</h3>
        <p>${err.message}</p>
      </div>
    `;
  }
}

// ---------- Render Journeys ----------
function renderJourneys(container, journeys) {
  container.innerHTML = journeys
    .map((j, idx) => {
      const isDirect = j.transfers === 0;
      const leg1 = j.legs[0];
      const needsClassChange = j.legs.some((l) => l.needs_class_change);
      const isFastest = idx === 0 && state.sort === "fastest";

      return `
      <div class="journey-card ${isFastest ? "featured" : ""}">
        <!-- Top Info Bar -->
        <div class="card-top-bar">
          <div class="train-title-group">
            <div class="train-title-row">
              <span class="train-badge-num">${isDirect ? leg1.train : `${j.legs.length} Trains`}</span>
              <span class="train-name-txt">${isDirect ? leg1.train_name : `Transfer Route via ${j.legs[0].to_name}`}</span>
            </div>
          </div>
          <div class="journey-badges-group">
            ${j.total_fare_inr ? `<span class="pill-badge pill-price">₹${j.total_fare_inr}</span>` : ""}
            ${isFastest ? '<span class="pill-badge pill-fastest">⚡ Fastest</span>' : ""}
            ${isDirect ? '<span class="pill-badge pill-direct">Direct</span>' : `<span class="pill-badge pill-transfers">${j.transfers} Transfer</span>`}
          </div>
        </div>

        <!-- Times & Route Track Visual -->
        <div class="route-summary-row">
          <div class="time-box">
            <span class="time-val">${j.departs}</span>
            <span class="station-sub">${leg1.from_name} (${leg1.from_code})</span>
            <span class="date-tag">${formatDateStr(leg1.date)}</span>
          </div>

          <div class="route-track-visual">
            <span class="duration-tag">⏱️ ${formatMinutes(j.duration_min)}</span>
            <div class="track-line"></div>
            <span class="duration-tag">${isDirect ? "Non-stop" : `${j.transfers} Station Change`}</span>
          </div>

          <div class="time-box right">
            <span class="time-val">${j.arrives}</span>
            <span class="station-sub">${j.legs[j.legs.length - 1].to_name} (${j.legs[j.legs.length - 1].to_code})</span>
            <span class="date-tag">${formatDateStr(j.arrives_date)}</span>
          </div>
        </div>

        <!-- Visual Segmented Bar if Train requires Mid-Route Class Change -->
        ${renderSegmentedBar(j)}

        <!-- Multi-train Timeline & Layover Badges if transfers > 0 -->
        ${renderTransfersTimeline(j)}

        <!-- Bottom Action Bar (Seats & Booking) -->
        <div class="card-bottom-bar">
          <div class="class-chips-list">
            ${renderClassChips(j, idx)}
          </div>
          <div style="display: flex; gap: 0.5rem;">
            <button class="btn btn-secondary btn-sm" onclick="inspectTrainDetails('${leg1.train}', '${leg1.date}', '${leg1.from_code}', '${leg1.to_code}')">
              🔍 Inspect Stops
            </button>
            <button class="btn btn-primary btn-sm" onclick="initiateBooking(${idx})">
              🎫 Book Ticket
            </button>
          </div>
        </div>
      </div>
    `;
    })
    .join("");
}

// Visual Segmented Bar for Mid-Route Class Change
function renderSegmentedBar(journey) {
  const splitLeg = journey.legs.find((l) => l.needs_class_change && l.class_plan && l.class_plan.length > 0);
  if (!splitLeg) return "";

  const plan = splitLeg.class_plan;

  return `
    <div class="segmented-plan-container">
      <div class="segmented-plan-header">
        <span class="segmented-plan-title">
          <span>⚡ Mid-Route Class Change Required (Same Train)</span>
        </span>
        <span class="coach-change-pill">${plan.length} Coach Stretches</span>
      </div>

      <div class="segmented-bar">
        ${plan
          .map((slice) => {
            const cls = slice.travel_class.toLowerCase();
            return `
            <div class="segment-slice slice-${cls}" title="${slice.travel_class}: ${slice.from_name} to ${slice.to_name}">
              <div class="slice-route">
                <span class="slice-badge">${slice.travel_class}</span>
                <span>${slice.from_code} ➔ ${slice.to_code}</span>
              </div>
              <div class="slice-seats">
                <span>${slice.seats} seats (${slice.departs} - ${slice.arrives})</span>
                ${slice.fare_inr ? `<span class="slice-fare-tag"> • ₹${slice.fare_inr}</span>` : ""}
              </div>
            </div>
          `;
          })
          .join("")}
      </div>

      <div class="coach-change-notice">
        <span>🔄 <strong>Coach Switch:</strong> Ride in <strong>${plan[0].travel_class}</strong>${plan[0].fare_inr ? ` (₹${plan[0].fare_inr})` : ""} from ${plan[0].from_name}, then switch coach to <strong>${plan[1]?.travel_class || "next"}</strong>${plan[1]?.fare_inr ? ` (₹${plan[1].fare_inr})` : ""} at <strong>${plan[0].to_name}</strong> (${plan[0].arrives}).</span>
      </div>
    </div>
  `;
}

// Multi-Train Transfers Timeline with Layover Badges
function renderTransfersTimeline(journey) {
  if (journey.transfers === 0) return "";

  let html = `<div class="transfer-timeline">`;

  journey.legs.forEach((leg, index) => {
    html += `
      <div class="transfer-leg-card">
        <div>
          <span class="train-badge-num">${leg.train}</span>
          <strong style="margin-left: 0.5rem;">${leg.train_name}</strong>
          <div style="font-size: 0.78rem; color: var(--text-secondary); margin-top: 0.2rem;">
            ${leg.from_name} (${leg.departs}) ➔ ${leg.to_name} (${leg.arrives})
          </div>
        </div>
        <div style="font-size: 0.75rem; text-align: right; color: var(--accent-emerald);">
          ${Object.entries(leg.seats)
            .map(([c, n]) => `${c}: ${n}`)
            .join(" • ") || (leg.needs_class_change ? "Class Change Plan" : "Available")}
        </div>
      </div>
    `;

    // Layover badge between legs
    if (index < journey.legs.length - 1) {
      const layoverMin = journey.layovers_min[index] || 60;
      html += `
        <div class="layover-badge-row">
          <div class="layover-badge">
            <span>⏳ Layover at ${leg.to_name} (${leg.to_code}): ${formatMinutes(layoverMin)}</span>
            <span style="font-size: 0.7rem; opacity: 0.85;">(Train change: ${leg.arrives} ➔ ${journey.legs[index + 1].departs})</span>
          </div>
        </div>
      `;
    }
  });

  html += `</div>`;
  return html;
}

// Class Chips Render
function renderClassChips(journey, journeyIdx) {
  const leg = journey.legs[0];
  if (leg.needs_class_change) {
    const planNames = leg.class_plan.map((p) => `${p.travel_class}${p.fare_inr ? ` (₹${p.fare_inr})` : ""}`).join(" ➔ ");
    return `
      <div class="class-seat-chip active">
        <span class="chip-cls-name">Split Plan:</span>
        <span class="chip-seat-avail">${planNames}</span>
        <span class="chip-fare">Total: ₹${journey.total_fare_inr || leg.fare_inr}</span>
      </div>
    `;
  }

  const entries = Object.entries(leg.seats);
  if (entries.length === 0) {
    return `<span style="font-size:0.75rem; color: var(--text-muted);">Check availability in booking</span>`;
  }

  return entries
    .map(
      ([cls, count]) => {
        const fare = leg.class_fares && leg.class_fares[cls] ? ` • ₹${leg.class_fares[cls]}` : "";
        return `
    <div class="class-seat-chip" onclick="selectClassForBooking(${journeyIdx}, '${cls}')">
      <span class="chip-cls-name">${cls}</span>
      <span class="chip-seat-avail ${count < 5 ? "low" : ""}">${count} seats</span>
      ${fare ? `<span class="chip-fare">${fare}</span>` : ""}
    </div>
  `;
      }
    )
    .join("");
}

function selectClassForBooking(journeyIdx, cls) {
  state.selectedClass = cls;
  initiateBooking(journeyIdx, cls);
}

// ---------- Booking Modal Flow ----------
function initiateBooking(journeyIndex, preClass = null) {
  const journey = state.journeys[journeyIndex];
  if (!journey) return;

  state.selectedJourney = journey;
  const isSplit = journey.legs.some((l) => l.needs_class_change && l.class_plan);
  const leg0 = journey.legs[0];
  const availableClasses = Object.keys(leg0.seats || {});
  const defaultClass = preClass || (availableClasses.length > 0 ? availableClasses[0] : "3A");
  state.selectedClass = defaultClass;

  const modal = document.getElementById("bookingModal");
  const modalBody = document.getElementById("bookingModalBody");
  if (!modal || !modalBody) return;

  function computeTotalFare(cls) {
    if (isSplit) {
      return journey.total_fare_inr || leg0.fare_inr || 0;
    }
    let total = 0;
    journey.legs.forEach((l) => {
      total += (l.class_fares && l.class_fares[cls]) || l.fare_inr || 0;
    });
    return total || journey.total_fare_inr || 0;
  }

  function renderBreakdownHtml(cls) {
    if (isSplit) {
      return leg0.class_plan
        .map(
          (p) => `
        <div class="fare-stretch-item">
          <span><strong>${p.travel_class}</strong>: ${p.from_code} ➔ ${p.to_code} ${p.fare_breakdown ? `(${p.fare_breakdown.distance_km} km)` : ""}</span>
          <span style="color:#fbbf24; font-weight:700;">₹${p.fare_inr}</span>
        </div>
      `
        )
        .join("");
    }
    return journey.legs
      .map(
        (l) => `
      <div class="fare-stretch-item">
        <span><strong>${l.train}</strong>: ${l.from_code} ➔ ${l.to_code} (${cls})</span>
        <span style="color:#fbbf24; font-weight:700;">₹${(l.class_fares && l.class_fares[cls]) || l.fare_inr || computeTotalFare(cls)}</span>
      </div>
    `
      )
      .join("");
  }

  const initialFare = computeTotalFare(state.selectedClass);

  modalBody.innerHTML = `
    <div class="booking-summary-box">
      <div class="summary-row">
        <span class="summary-label">Route</span>
        <span class="summary-val">${journey.legs[0].from_name} ➔ ${journey.legs[journey.legs.length - 1].to_name}</span>
      </div>
      <div class="summary-row">
        <span class="summary-label">Departure</span>
        <span class="summary-val">${journey.departs} (${formatDateStr(journey.legs[0].date)})</span>
      </div>
      <div class="summary-row">
        <span class="summary-label">Total Duration</span>
        <span class="summary-val">${formatMinutes(journey.duration_min)} (${journey.transfers === 0 ? "Direct" : `${journey.transfers} Transfers`})</span>
      </div>
      ${
        isSplit
          ? `
        <div class="summary-row">
          <span class="summary-label">Class Plan</span>
          <span class="summary-val" style="color:var(--accent-amber)">${leg0.class_plan.map((p) => `${p.travel_class} (${p.from_code} - ${p.to_code})`).join(" + ")}</span>
        </div>
      `
          : `
        <div class="summary-row">
          <span class="summary-label">Class</span>
          <select id="modalClassSelect" class="form-input no-icon" style="width: auto; padding: 0.2rem 0.5rem;">
            ${(availableClasses.length > 0 ? availableClasses : ["3A", "2A", "SL"])
              .map(
                (c) =>
                  `<option value="${c}" ${state.selectedClass === c ? "selected" : ""}>${c} (${leg0.seats?.[c] ? leg0.seats[c] + " seats" : "Available"}${leg0.class_fares?.[c] ? " • ₹" + leg0.class_fares[c] : ""})</option>`
              )
              .join("")}
          </select>
        </div>
      `
      }
      <div class="summary-row">
        <span class="summary-label">Total Ticket Fare</span>
        <span class="summary-val" id="modalTotalFare" style="color:#fbbf24; font-weight:800; font-size:1.15rem;">₹${initialFare}</span>
      </div>

      <div class="fare-breakdown-box">
        <span style="font-size:0.75rem; font-weight:700; color:var(--text-secondary); text-transform:uppercase;">Price Breakdown per Stretch</span>
        <div id="modalBreakdownItems">
          ${renderBreakdownHtml(state.selectedClass)}
        </div>
      </div>
    </div>

    <div class="form-group" style="margin-top: 1rem;">
      <label class="form-label" for="passengerNameInput">Passenger Full Name *</label>
      <input type="text" id="passengerNameInput" class="form-input no-icon" placeholder="e.g. Aditi Sharma" required />
    </div>

    <div style="font-size: 0.75rem; color: var(--text-muted); background: var(--bg-tertiary); padding: 0.65rem 0.85rem; border-radius: var(--radius-sm); border: 1px solid var(--border-color); margin-top:0.75rem;">
      ℹ️ <strong>Simulated Booking:</strong> Instant reservation against local engine database. No actual payment required.
    </div>
  `;

  const classSelect = document.getElementById("modalClassSelect");
  if (classSelect) {
    classSelect.addEventListener("change", (e) => {
      const newCls = e.target.value;
      state.selectedClass = newCls;
      const newFare = computeTotalFare(newCls);
      const fareEl = document.getElementById("modalTotalFare");
      const breakdownEl = document.getElementById("modalBreakdownItems");
      if (fareEl) fareEl.innerText = `₹${newFare}`;
      if (breakdownEl) breakdownEl.innerHTML = renderBreakdownHtml(newCls);
    });
  }

  modal.style.display = "flex";
}

function closeBookingModal() {
  const modal = document.getElementById("bookingModal");
  if (modal) modal.style.display = "none";
}

async function confirmBookingSubmit() {
  const passengerInput = document.getElementById("passengerNameInput");
  const name = passengerInput ? passengerInput.value.trim() : "";

  if (!name) {
    alert("Please enter a passenger name to proceed with booking.");
    passengerInput?.focus();
    return;
  }

  const journey = state.selectedJourney;
  if (!journey) return;

  const isSplit = journey.legs.some((l) => l.needs_class_change && l.class_plan);
  const selectedCls = isSplit ? null : document.getElementById("modalClassSelect")?.value || state.selectedClass || "3A";

  let legsToBook = [];
  if (isSplit) {
    // Pass one leg per stretch of class_plan
    const splitLeg = journey.legs[0];
    legsToBook = splitLeg.class_plan.map((p) => ({
      train: splitLeg.train,
      from_code: p.from_code,
      to_code: p.to_code,
      date: p.date,
      travel_class: p.travel_class,
    }));
  } else {
    // Normal single or multi-leg booking
    legsToBook = journey.legs.map((leg) => ({
      train: leg.train,
      from_code: leg.from_code,
      to_code: leg.to_code,
      date: leg.date,
      travel_class: selectedCls,
    }));
  }

  const submitBtn = document.getElementById("confirmBookingBtn");
  if (submitBtn) {
    submitBtn.disabled = true;
    submitBtn.innerHTML = `<span class="spinner"></span> Confirming...`;
  }

  try {
    const res = await fetch("/api/book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        legs: legsToBook,
        passenger_name: name,
        travel_class: selectedCls,
      }),
    });

    const data = await parseJsonResponse(res);
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerText = "Confirm Booking";
    }

    if (data.success && data.booking) {
      renderBookingSuccess(data.booking);
      refreshBookings();
    } else {
      alert(`Booking Failed: ${data.reason || "Please try another train or date."}`);
    }
  } catch (err) {
    if (submitBtn) {
      submitBtn.disabled = false;
      submitBtn.innerText = "Confirm Booking";
    }
    alert(`Error: ${err.message}`);
  }
}

function renderBookingSuccess(booking) {
  const modalBody = document.getElementById("bookingModalBody");
  const modalFooter = document.getElementById("bookingModalFooter");

  if (!modalBody) return;

  const totalFareDisplay = booking.total_fare_inr ? `₹${booking.total_fare_inr}` : (booking.fare_inr ? `₹${booking.fare_inr}` : "");

  modalBody.innerHTML = `
    <div class="pnr-showcase">
      <span style="font-size:0.8rem; font-weight:700; text-transform:uppercase; color:var(--accent-emerald);">Booking Confirmed!</span>
      <span class="pnr-code">${booking.pnr}</span>
      <span style="font-size:0.85rem; color:var(--text-primary);">Passenger: <strong>${booking.passenger}</strong></span>
    </div>

    <div class="booking-summary-box">
      ${
        totalFareDisplay
          ? `
      <div class="summary-row">
        <span class="summary-label">Total Ticket Fare</span>
        <span class="summary-val" style="color:#fbbf24; font-weight:800; font-size:1.15rem;">${totalFareDisplay}</span>
      </div>`
          : ""
      }
      <div class="summary-row">
        <span class="summary-label">Class</span>
        <span class="summary-val">${booking.travel_class}</span>
      </div>
      <div class="summary-row">
        <span class="summary-label">Booked At</span>
        <span class="summary-val">${booking.booked_at}</span>
      </div>
      <div class="summary-row">
        <span class="summary-label">Legs Count</span>
        <span class="summary-val">${booking.legs.length} Leg(s)</span>
      </div>
      ${
        booking.legs && booking.legs.length > 0
          ? `
        <div class="fare-breakdown-box" style="margin-top:0.6rem;">
          <span style="font-size:0.75rem; font-weight:700; color:var(--text-secondary); text-transform:uppercase;">Stretch Fare Breakdown</span>
          ${booking.legs
            .map(
              (l) => `
            <div class="fare-stretch-item">
              <span><strong>${l.train}</strong> (${l.from_code} ➔ ${l.to_code}) • ${l.travel_class}</span>
              <span style="color:#fbbf24; font-weight:700;">₹${l.fare_inr || "N/A"}</span>
            </div>
          `
            )
            .join("")}
        </div>
      `
          : ""
      }
    </div>
  `;

  if (modalFooter) {
    modalFooter.innerHTML = `
      <button class="btn btn-secondary" onclick="closeBookingModal()">Done</button>
      <button class="btn btn-primary" onclick="closeBookingModal(); openBookingsModal();">View in My Bookings</button>
    `;
  }
}

// ---------- My Bookings Management ----------
async function refreshBookings() {
  try {
    const res = await fetch("/api/bookings");
    const data = await parseJsonResponse(res);
    if (data.success && Array.isArray(data.bookings)) {
      state.activeBookings = data.bookings;
      const countEl = document.getElementById("activeBookingsCount");
      if (countEl) countEl.innerText = data.bookings.length;
    }
  } catch (err) {
    console.error("Refresh bookings error:", err);
  }
}

function openBookingsModal() {
  const modal = document.getElementById("bookingsListModal");
  const container = document.getElementById("bookingsListContainer");
  if (!modal || !container) return;

  if (state.activeBookings.length === 0) {
    container.innerHTML = `
      <div class="empty-state">
        <div class="empty-icon">🎟️</div>
        <p>No active bookings found.</p>
      </div>
    `;
  } else {
    container.innerHTML = state.activeBookings
      .map(
        (b) => `
      <div class="transfer-leg-card" style="margin-bottom: 0.75rem;">
        <div>
          <div style="display:flex; align-items:center; gap:0.5rem;">
            <span class="pnr-code" style="font-size:1.1rem;">${b.pnr}</span>
            <span class="pill-badge pill-direct">${b.travel_class}</span>
          </div>
          <div style="font-size: 0.85rem; color: var(--text-primary); margin-top: 0.25rem;">
            Passenger: <strong>${b.passenger}</strong> ${b.total_fare_inr ? `• <span style="color:#fbbf24; font-weight:700;">₹${b.total_fare_inr}</span>` : ""}
          </div>
          <div style="font-size: 0.75rem; color: var(--text-muted); margin-top: 0.15rem;">
            ${b.legs.map((l) => `${l.train} (${l.from_code} ➔ ${l.to_code}) on ${l.date}`).join(" • ")}
          </div>
        </div>
        <button class="btn btn-danger btn-sm" onclick="cancelBooking('${b.pnr}')">
          Cancel Ticket
        </button>
      </div>
    `
      )
      .join("");
  }

  modal.style.display = "flex";
}

function closeBookingsModal() {
  const modal = document.getElementById("bookingsListModal");
  if (modal) modal.style.display = "none";
}

async function cancelBooking(pnr) {
  if (!confirm(`Are you sure you want to cancel booking ${pnr}?`)) return;

  try {
    const res = await fetch("/api/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pnr: pnr }),
    });
    const data = await parseJsonResponse(res);
    if (data.success) {
      alert(`Booking ${pnr} has been cancelled.`);
      await refreshBookings();
      openBookingsModal();
    } else {
      alert(`Cancel failed: ${data.reason}`);
    }
  } catch (err) {
    alert(`Error: ${err.message}`);
  }
}

// ---------- Check Seats Detailed Stops Inspection ----------
async function inspectTrainDetails(train, date, from, to) {
  try {
    const params = new URLSearchParams({
      train_number: train,
      travel_date: date,
      from_station: from || "",
      to_station: to || "",
    });
    const res = await fetch(`/api/check-seats?${params.toString()}`);
    const data = await parseJsonResponse(res);

    if (!data.success) {
      alert(`Seat check: ${data.reason}`);
      return;
    }

    const modal = document.getElementById("genericModal");
    const title = document.getElementById("genericModalTitle");
    const body = document.getElementById("genericModalBody");

    if (title) title.innerText = `Train ${train} Availability (${from} ➔ ${to})`;
    if (body) {
      body.innerHTML = `
        <div class="booking-summary-box">
          <div class="summary-row">
            <span class="summary-label">Full Route Coverage</span>
            <span class="summary-val">${Object.entries(data.seats).map(([c, n]) => `${c}: ${n} seats`).join(" • ") || "No single class covers entire trip"}</span>
          </div>
        </div>

        ${
          data.class_plan && data.class_plan.length > 0
            ? `
          <h4 style="font-size:0.88rem; color:var(--accent-amber); margin-top:0.5rem;">Suggested Class Change Plan:</h4>
          <div class="segmented-bar">
            ${data.class_plan
              .map(
                (p) => `
              <div class="segment-slice slice-${p.travel_class.toLowerCase()}">
                <span>${p.travel_class}</span>
                <span>${p.from_name} ➔ ${p.to_name} (${p.seats} seats)</span>
              </div>
            `
              )
              .join("")}
          </div>
        `
            : ""
        }

        <h4 style="font-size:0.88rem; color:var(--text-secondary); margin-top:0.75rem;">Stretches per Class:</h4>
        <div style="max-height: 200px; overflow-y:auto; display:flex; flex-direction:column; gap:0.4rem;">
          ${Object.entries(data.stretches || {})
            .map(
              ([cls, list]) => `
            <div style="font-size:0.78rem; background:var(--bg-secondary); padding:0.4rem 0.65rem; border-radius:var(--radius-sm);">
              <strong>${cls}:</strong> ${list.map((s) => `${s.from_code} ➔ ${s.to_code} (${s.seats} seats)`).join(" • ") || "None"}
            </div>
          `
            )
            .join("")}
        </div>
      `;
    }

    if (modal) modal.style.display = "flex";
  } catch (err) {
    alert(`Inspection error: ${err.message}`);
  }
}

function closeGenericModal() {
  const modal = document.getElementById("genericModal");
  if (modal) modal.style.display = "none";
}

// ---------- AI Chat Assistant ----------
async function handleChatSubmit() {
  const input = document.getElementById("chatInput");
  const sendBtn = document.getElementById("chatSendBtn");
  const msgText = input ? input.value.trim() : "";
  if (!msgText) return;

  input.value = "";
  appendChatMessage("user", msgText);

  state.isChatting = true;
  if (sendBtn) {
    sendBtn.disabled = true;
    sendBtn.innerHTML = `<span class="spinner" style="width:14px;height:14px;"></span>`;
  }

  // Temporary thinking indicator
  const thinkingId = appendChatThinking();

  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: msgText,
        history: state.chatHistory,
      }),
    });

    const data = await parseJsonResponse(res);
    removeChatThinking(thinkingId);

    state.isChatting = false;
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.innerText = "Send";
    }

    if (data.history) {
      state.chatHistory = data.history;
    }

    appendChatMessage("agent", data.reply || "Done.", data.tool_calls || []);

    // If bookings changed via chat, refresh counter
    if (data.tool_calls && data.tool_calls.some((tc) => tc.name === "book_ticket" || tc.name === "cancel_ticket")) {
      refreshBookings();
    }
  } catch (err) {
    removeChatThinking(thinkingId);
    state.isChatting = false;
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.innerText = "Send";
    }
    appendChatMessage("agent", `⚠️ Agent communication error: ${err.message}`);
  }
}

function appendChatMessage(role, content, toolCalls = []) {
  const messagesBox = document.getElementById("chatMessages");
  if (!messagesBox) return;

  const msgDiv = document.createElement("div");
  msgDiv.className = `chat-msg ${role}`;

  let toolsHtml = "";
  if (toolCalls && toolCalls.length > 0) {
    toolsHtml = toolCalls
      .map(
        (tc) => `
      <div class="tool-call-card" title="${JSON.stringify(tc.args)}">
        <span>🔧 <strong>${tc.name}</strong>(${Object.entries(tc.args)
          .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
          .join(", ")})</span>
      </div>
    `
      )
      .join("");
  }

  // Basic markdown-like parser (bold, bullet points, line breaks)
  let formattedContent = escapeHtml(content)
    .replace(/\*\*(.*?)\*\*/g, "<strong>$1</strong>")
    .replace(/\*(.*?)\*/g, "<em>$1</em>")
    .replace(/\n\n/g, "</p><p>")
    .replace(/\n\* /g, "<br>• ")
    .replace(/\n- /g, "<br>• ")
    .replace(/\n/g, "<br>");

  msgDiv.innerHTML = `
    <div class="msg-bubble">
      <p>${formattedContent}</p>
      ${toolsHtml}
    </div>
  `;

  messagesBox.appendChild(msgDiv);
  messagesBox.scrollTop = messagesBox.scrollHeight;
}

function appendChatThinking() {
  const messagesBox = document.getElementById("chatMessages");
  if (!messagesBox) return null;

  const id = `thinking_${Date.now()}`;
  const div = document.createElement("div");
  div.id = id;
  div.className = "chat-msg agent";
  div.innerHTML = `
    <div class="msg-bubble" style="opacity: 0.75; display:flex; align-items:center; gap:0.5rem;">
      <span class="spinner" style="width:14px;height:14px;"></span>
      <span>Thinking & searching routes...</span>
    </div>
  `;
  messagesBox.appendChild(div);
  messagesBox.scrollTop = messagesBox.scrollHeight;
  return id;
}

function removeChatThinking(id) {
  if (!id) return;
  const el = document.getElementById(id);
  if (el) el.remove();
}

function escapeHtml(text) {
  if (!text) return "";
  const map = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  };
  return text.replace(/[&<>"']/g, (m) => map[m]);
}
