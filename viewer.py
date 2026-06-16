import argparse
import json
from collections import defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent
DIARY_PATH = BASE_DIR / "data" / "diary.jsonl"


def read_entries() -> list[dict]:
    if not DIARY_PATH.exists():
        return []

    entries = []
    with DIARY_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


def entry_diary_date(entry: dict) -> str:
    return str(entry.get("diary_date") or entry.get("message_sent_date") or "без даты")


def entry_time(entry: dict) -> str:
    sent_at = entry.get("message_sent_at") or entry.get("created_at") or ""
    if not sent_at:
        return ""
    try:
        return datetime.fromisoformat(sent_at).strftime("%H:%M")
    except ValueError:
        return sent_at


def checkin_answers(entry: dict) -> dict:
    raw = entry.get("raw") or {}
    answers = raw.get("answers") or {}
    return answers if isinstance(answers, dict) else {}


def normalize_entry(entry: dict) -> dict:
    entry_type = str(entry.get("type") or "entry")
    summary = str(entry.get("summary") or "")
    raw = entry.get("raw") or {}
    answers = checkin_answers(entry)
    return {
        "type": entry_type,
        "diary_date": entry_diary_date(entry),
        "time": entry_time(entry),
        "summary": summary,
        "message_sent_at": entry.get("message_sent_at") or entry.get("created_at") or "",
        "received_at": entry.get("received_at") or "",
        "raw_text": raw.get("text") or "",
        "answers": answers,
        "pain_level": answers.get("pain_level"),
        "sleep_quality": answers.get("sleep_quality"),
        "fell_asleep_at": answers.get("fell_asleep_at"),
        "did_sport": answers.get("did_sport"),
    }


def build_payload() -> dict:
    entries = [normalize_entry(entry) for entry in read_entries()]
    entries.sort(key=lambda item: (item["diary_date"], item["message_sent_at"]))

    days: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        days[entry["diary_date"]].append(entry)

    day_summaries = []
    for diary_date, day_entries in sorted(days.items(), reverse=True):
        checkins = [entry for entry in day_entries if entry["type"] == "daily_checkin"]
        fasting = any(entry["type"] == "fasting_day" for entry in day_entries)
        pain_values = [
            entry["pain_level"]
            for entry in checkins
            if isinstance(entry.get("pain_level"), (int, float))
        ]
        day_summaries.append(
            {
                "date": diary_date,
                "count": len(day_entries),
                "has_checkin": bool(checkins),
                "fasting": fasting,
                "pain": pain_values[-1] if pain_values else None,
            }
        )

    return {
        "entries": entries,
        "days": day_summaries,
        "total_entries": len(entries),
        "total_days": len(days),
    }


def json_response(handler: BaseHTTPRequestHandler, payload: dict, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def html_response(handler: BaseHTTPRequestHandler, body: str, status: int = 200) -> None:
    encoded = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def page_html() -> str:
    return """<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Дневник питания и самочувствия</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #1d2430;
      --muted: #687386;
      --line: #dce1e8;
      --accent: #246bfe;
      --accent-soft: #eaf1ff;
      --danger-soft: #fff1f0;
      --ok-soft: #eaf8ef;
      --radius: 8px;
      font-family: Inter, Segoe UI, system-ui, -apple-system, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-size: 15px;
    }
    .layout {
      display: grid;
      grid-template-columns: 320px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      border-right: 1px solid var(--line);
      background: var(--panel);
      padding: 18px;
      position: sticky;
      top: 0;
      height: 100vh;
      overflow: auto;
    }
    main {
      padding: 24px;
      max-width: 1100px;
      width: 100%;
    }
    h1 {
      font-size: 22px;
      margin: 0 0 4px;
      letter-spacing: 0;
    }
    h2 {
      font-size: 18px;
      margin: 28px 0 12px;
      letter-spacing: 0;
    }
    .muted { color: var(--muted); }
    .controls {
      display: grid;
      gap: 10px;
      margin: 18px 0;
    }
    input, select, button {
      width: 100%;
      border: 1px solid var(--line);
      background: #fff;
      color: var(--text);
      padding: 10px 11px;
      border-radius: var(--radius);
      font: inherit;
    }
    button {
      cursor: pointer;
      text-align: left;
    }
    button.active {
      border-color: var(--accent);
      background: var(--accent-soft);
      color: #0f48d8;
      font-weight: 600;
    }
    .stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin: 16px 0;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 10px;
      background: #fbfcfe;
    }
    .stat strong {
      display: block;
      font-size: 20px;
      margin-bottom: 2px;
    }
    .day-list {
      display: grid;
      gap: 8px;
      margin-top: 12px;
    }
    .day-button {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
    }
    .badge-row {
      display: flex;
      gap: 5px;
      flex-wrap: wrap;
      justify-content: flex-end;
    }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      border-radius: 999px;
      padding: 2px 8px;
      background: #eef1f5;
      color: #4d596b;
      font-size: 12px;
      white-space: nowrap;
    }
    .badge.pain { background: var(--danger-soft); color: #b42318; }
    .badge.checkin { background: var(--accent-soft); color: #0f48d8; }
    .badge.fasting { background: var(--ok-soft); color: #18703b; }
    .badge.warn { background: #fff7e6; color: #9a5b00; }
    .entry {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 14px 16px;
      margin-bottom: 10px;
    }
    .entry-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
    }
    .entry-type {
      font-weight: 700;
    }
    .entry-time {
      color: var(--muted);
      white-space: nowrap;
    }
    .summary {
      white-space: pre-wrap;
      line-height: 1.45;
    }
    .checkin-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(120px, 1fr));
      gap: 8px;
      margin-top: 8px;
    }
    .checkin-cell {
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 9px;
      background: #fbfcfe;
    }
    .checkin-cell span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 3px;
    }
    .empty {
      border: 1px dashed var(--line);
      border-radius: var(--radius);
      padding: 24px;
      background: #fff;
      color: var(--muted);
    }
    .analytics {
      display: grid;
      gap: 14px;
      margin-bottom: 22px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: var(--radius);
      padding: 16px;
    }
    .panel h2 {
      margin-top: 0;
    }
    .chart-wrap {
      width: 100%;
      overflow-x: auto;
    }
    .pain-chart {
      width: 100%;
      min-width: 640px;
      height: 260px;
      display: block;
    }
    .chart-grid {
      stroke: #e8edf3;
      stroke-width: 1;
    }
    .chart-axis {
      stroke: #aab4c2;
      stroke-width: 1;
    }
    .chart-line {
      fill: none;
      stroke: #d92d20;
      stroke-width: 2.5;
    }
    .chart-dot {
      fill: #d92d20;
      stroke: #fff;
      stroke-width: 2;
    }
    .chart-fasting {
      fill: #2f9e44;
      opacity: 0.18;
    }
    .chart-label {
      fill: var(--muted);
      font-size: 12px;
    }
    .chart-empty {
      color: var(--muted);
      padding: 18px 0 4px;
    }
    .table-wrap {
      overflow-x: auto;
    }
    table {
      border-collapse: collapse;
      width: 100%;
      min-width: 680px;
      font-size: 14px;
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 8px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      background: #fbfcfe;
    }
    tr:last-child td {
      border-bottom: 0;
    }
    .note {
      margin-top: 10px;
      color: var(--muted);
      line-height: 1.45;
    }
    .trigger-high {
      color: #b42318;
      font-weight: 700;
    }
    .trigger-low {
      color: #18703b;
      font-weight: 700;
    }
    @media (max-width: 820px) {
      .layout { grid-template-columns: 1fr; }
      aside {
        position: static;
        height: auto;
        border-right: 0;
        border-bottom: 1px solid var(--line);
      }
      main { padding: 16px; }
      .checkin-grid { grid-template-columns: 1fr 1fr; }
      .pain-chart { min-width: 560px; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <h1>Дневник</h1>
      <div class="muted" id="updated">Загрузка...</div>
      <div class="stats">
        <div class="stat"><strong id="totalDays">0</strong><span class="muted">дней</span></div>
        <div class="stat"><strong id="totalEntries">0</strong><span class="muted">записей</span></div>
      </div>
      <div class="controls">
        <input id="search" type="search" placeholder="Поиск по дневнику">
        <select id="typeFilter">
          <option value="all">Все записи</option>
          <option value="text">Еда и текст</option>
          <option value="daily_checkin">Чек-ины</option>
          <option value="fasting_day">Разгрузочные дни</option>
          <option value="voice">Голосовые</option>
          <option value="photo">Фото</option>
        </select>
        <button id="allDays" class="active">Все дни</button>
      </div>
      <div class="muted">Дни</div>
      <div class="day-list" id="dayList"></div>
    </aside>
    <main>
      <section class="analytics">
        <div class="panel">
          <h2>Боль по дням</h2>
          <div class="chart-wrap" id="painChart"></div>
        </div>
        <div class="panel">
          <h2>Чек-ины</h2>
          <div class="table-wrap" id="checkinTable"></div>
        </div>
        <div class="panel">
          <h2>Возможные триггеры</h2>
          <div class="table-wrap" id="triggerTable"></div>
          <div class="note">Это не медицинский вывод, а поиск совпадений в дневнике. Сильные сигналы стоит проверять отдельно и обсуждать с врачом.</div>
        </div>
      </section>
      <div id="content" class="empty">Загрузка дневника...</div>
    </main>
  </div>
  <script>
    const typeNames = {
      text: "Запись",
      daily_checkin: "Чек-ин",
      fasting_day: "Разгрузочный день",
      voice: "Голос",
      photo: "Фото"
    };
    let payload = { entries: [], days: [] };
    let selectedDay = "all";

    const els = {
      updated: document.querySelector("#updated"),
      totalDays: document.querySelector("#totalDays"),
      totalEntries: document.querySelector("#totalEntries"),
      search: document.querySelector("#search"),
      typeFilter: document.querySelector("#typeFilter"),
      allDays: document.querySelector("#allDays"),
      dayList: document.querySelector("#dayList"),
      painChart: document.querySelector("#painChart"),
      checkinTable: document.querySelector("#checkinTable"),
      triggerTable: document.querySelector("#triggerTable"),
      content: document.querySelector("#content")
    };

    const triggerGroups = [
      { name: "сладкое", keywords: ["сахар", "торт", "тортик", "пирож", "печенье", "конфет", "шоколад", "сладк"] },
      { name: "глютен / хлеб", keywords: ["хлеб", "хлебец", "глютен", "пшениц", "булк", "макарон", "пицц"] },
      { name: "молочные", keywords: ["молоко", "сыр", "йогурт", "кефир", "творог", "сливк"] },
      { name: "бобовые", keywords: ["фасоль", "горох", "нут", "чечевиц", "боб"] },
      { name: "пасленовые", keywords: ["помидор", "томат", "картоф", "баклажан", "перец"] },
      { name: "кофеин", keywords: ["чай", "кофе", "матча"] },
      { name: "фрукты", keywords: ["яблок", "груш", "персик", "лонган", "банан", "апельсин", "ягод"] },
      { name: "рис", keywords: ["рис"] },
      { name: "рыба", keywords: ["рыба", "минтай", "лосось", "тунец", "скумбр"] },
      { name: "курица", keywords: ["куриц", "наггет", "нагет"] }
    ];

    function escapeHtml(value) {
      return String(value ?? "")
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;");
    }

    function entryMatches(entry) {
      const query = els.search.value.trim().toLowerCase();
      const type = els.typeFilter.value;
      if (selectedDay !== "all" && entry.diary_date !== selectedDay) return false;
      if (type !== "all" && entry.type !== type) return false;
      if (!query) return true;
      const haystack = [
        entry.summary,
        entry.raw_text,
        entry.diary_date,
        entry.type,
        JSON.stringify(entry.answers || {})
      ].join(" ").toLowerCase();
      return haystack.includes(query);
    }

    function dayMap() {
      return new Map(payload.days.map(day => [day.date, day]));
    }

    function addDays(dateText, days) {
      const date = new Date(dateText + "T00:00:00");
      date.setDate(date.getDate() + days);
      return date.toISOString().slice(0, 10);
    }

    function average(values) {
      if (!values.length) return null;
      return values.reduce((sum, value) => sum + Number(value), 0) / values.length;
    }

    function formatNumber(value) {
      return value === null || value === undefined ? "мало данных" : Number(value).toFixed(1);
    }

    function exposureText(entry) {
      if (entry.type === "daily_checkin" || entry.type === "fasting_day") return "";
      return [entry.summary, entry.raw_text].join(" ").toLowerCase();
    }

    function renderDayList() {
      els.allDays.classList.toggle("active", selectedDay === "all");
      els.dayList.innerHTML = payload.days.map(day => {
        const badges = [
          day.has_checkin ? '<span class="badge checkin">чек-ин</span>' : "",
          day.fasting ? '<span class="badge fasting">разгрузка</span>' : "",
          day.pain !== null ? `<span class="badge pain">боль ${escapeHtml(day.pain)}/10</span>` : "",
          `<span class="badge">${escapeHtml(day.count)}</span>`
        ].join("");
        return `
          <button class="day-button ${selectedDay === day.date ? "active" : ""}" data-day="${escapeHtml(day.date)}">
            <span>${escapeHtml(day.date)}</span>
            <span class="badge-row">${badges}</span>
          </button>
        `;
      }).join("");
      document.querySelectorAll("[data-day]").forEach(button => {
        button.addEventListener("click", () => {
          selectedDay = button.dataset.day;
          render();
        });
      });
    }

    function renderCheckin(entry) {
      const a = entry.answers || {};
      return `
        <div class="checkin-grid">
          <div class="checkin-cell"><span>Сон</span>${escapeHtml(a.sleep_quality ?? "не указано")}/10</div>
          <div class="checkin-cell"><span>Уснул</span>${escapeHtml(a.fell_asleep_at ?? "не указано")}</div>
          <div class="checkin-cell"><span>Боль</span>${escapeHtml(a.pain_level ?? "не указано")}/10</div>
          <div class="checkin-cell"><span>Спорт</span>${a.did_sport === true ? "да" : a.did_sport === false ? "нет" : "не указано"}</div>
        </div>
      `;
    }

    function renderPainChart() {
      const series = payload.days
        .filter(day => day.pain !== null && day.pain !== undefined)
        .sort((a, b) => a.date.localeCompare(b.date));

      if (!series.length) {
        els.painChart.innerHTML = '<div class="chart-empty">Пока нет чек-инов с уровнем боли.</div>';
        return;
      }

      const width = Math.max(640, series.length * 70);
      const height = 250;
      const left = 42;
      const right = 18;
      const top = 18;
      const bottom = 42;
      const plotWidth = width - left - right;
      const plotHeight = height - top - bottom;
      const xFor = index => left + (series.length === 1 ? plotWidth / 2 : (index / (series.length - 1)) * plotWidth);
      const yFor = pain => top + ((10 - Number(pain)) / 10) * plotHeight;
      const path = series.map((day, index) => `${index === 0 ? "M" : "L"} ${xFor(index).toFixed(1)} ${yFor(day.pain).toFixed(1)}`).join(" ");
      const levels = [0, 2, 4, 6, 8, 10];
      const labelEvery = Math.max(1, Math.ceil(series.length / 8));

      const grid = levels.map(level => {
        const y = yFor(level);
        return `
          <line class="chart-grid" x1="${left}" y1="${y}" x2="${width - right}" y2="${y}"></line>
          <text class="chart-label" x="8" y="${y + 4}">${level}</text>
        `;
      }).join("");

      const fastingMarkers = series.map((day, index) => {
        if (!day.fasting) return "";
        const x = xFor(index) - 11;
        return `<rect class="chart-fasting" x="${x}" y="${top}" width="22" height="${plotHeight}" rx="4"></rect>`;
      }).join("");

      const dots = series.map((day, index) => {
        const x = xFor(index);
        const y = yFor(day.pain);
        return `
          <circle class="chart-dot" cx="${x}" cy="${y}" r="5">
            <title>${escapeHtml(day.date)}: боль ${escapeHtml(day.pain)}/10${day.fasting ? ", разгрузочный день" : ""}</title>
          </circle>
        `;
      }).join("");

      const xLabels = series.map((day, index) => {
        if (index % labelEvery !== 0 && index !== series.length - 1) return "";
        const x = xFor(index);
        return `<text class="chart-label" x="${x}" y="${height - 12}" text-anchor="middle">${escapeHtml(day.date.slice(5))}</text>`;
      }).join("");

      els.painChart.innerHTML = `
        <svg class="pain-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="График боли по дням">
          ${fastingMarkers}
          ${grid}
          <line class="chart-axis" x1="${left}" y1="${top + plotHeight}" x2="${width - right}" y2="${top + plotHeight}"></line>
          <line class="chart-axis" x1="${left}" y1="${top}" x2="${left}" y2="${top + plotHeight}"></line>
          <path class="chart-line" d="${path}"></path>
          ${dots}
          ${xLabels}
        </svg>
        <div class="muted">Зелёная подложка отмечает разгрузочные дни.</div>
      `;
    }

    function renderCheckinTable() {
      const days = dayMap();
      const checkins = payload.entries
        .filter(entry => entry.type === "daily_checkin")
        .sort((a, b) => (b.diary_date + b.message_sent_at).localeCompare(a.diary_date + a.message_sent_at));

      if (!checkins.length) {
        els.checkinTable.innerHTML = '<div class="chart-empty">Пока нет сохранённых чек-инов.</div>';
        return;
      }

      const rows = checkins.map(entry => {
        const a = entry.answers || {};
        const day = days.get(entry.diary_date) || {};
        const sport = a.did_sport === true ? "да" : a.did_sport === false ? "нет" : "не указано";
        return `
          <tr>
            <td>${escapeHtml(entry.diary_date)}</td>
            <td>${escapeHtml(a.sleep_quality ?? "не указано")}/10</td>
            <td>${escapeHtml(a.fell_asleep_at ?? "не указано")}</td>
            <td><strong>${escapeHtml(a.pain_level ?? "не указано")}/10</strong></td>
            <td>${sport}</td>
            <td>${day.fasting ? '<span class="badge fasting">да</span>' : '<span class="muted">нет</span>'}</td>
          </tr>
        `;
      }).join("");

      els.checkinTable.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Дата</th>
              <th>Сон</th>
              <th>Уснул</th>
              <th>Боль</th>
              <th>Спорт</th>
              <th>Разгрузка</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    }

    function analyzeTriggers() {
      const painByDate = new Map();
      payload.entries
        .filter(entry => entry.type === "daily_checkin" && Number.isFinite(Number(entry.pain_level)))
        .forEach(entry => painByDate.set(entry.diary_date, Number(entry.pain_level)));

      const allPainDates = [...painByDate.keys()].sort();
      if (allPainDates.length < 4) return [];

      const textByDate = new Map();
      payload.entries.forEach(entry => {
        const text = exposureText(entry);
        if (!text) return;
        textByDate.set(entry.diary_date, `${textByDate.get(entry.diary_date) || ""} ${text}`);
      });

      return triggerGroups.map(group => {
        const exposureDates = [...textByDate.entries()]
          .filter(([, text]) => group.keywords.some(keyword => text.includes(keyword)))
          .map(([date]) => date)
          .sort();

        const lagResults = [0, 1, 2].map(lag => {
          const exposedTargetDates = [...new Set(exposureDates.map(date => addDays(date, lag)))];
          const exposedPain = exposedTargetDates
            .filter(date => painByDate.has(date))
            .map(date => painByDate.get(date));
          const exposedTargetSet = new Set(exposedTargetDates);
          const baselinePain = allPainDates
            .filter(date => !exposedTargetSet.has(date))
            .map(date => painByDate.get(date));
          const exposedAvg = average(exposedPain);
          const baselineAvg = average(baselinePain);
          const diff = exposedPain.length >= 2 && baselinePain.length >= 2
            ? exposedAvg - baselineAvg
            : null;
          return {
            lag,
            exposedAvg,
            baselineAvg,
            diff,
            exposedCount: exposedPain.length,
            baselineCount: baselinePain.length
          };
        });

        const usable = lagResults.filter(result => result.diff !== null);
        const best = usable.length
          ? usable.sort((a, b) => b.diff - a.diff)[0]
          : lagResults.sort((a, b) => b.exposedCount - a.exposedCount)[0];

        return {
          name: group.name,
          occurrences: exposureDates.length,
          best,
          hasData: usable.length > 0
        };
      })
        .filter(result => result.occurrences > 0)
        .sort((a, b) => {
          if (a.hasData && b.hasData) return b.best.diff - a.best.diff;
          if (a.hasData) return -1;
          if (b.hasData) return 1;
          return b.occurrences - a.occurrences;
        });
    }

    function renderTriggerTable() {
      const results = analyzeTriggers();
      if (!results.length) {
        els.triggerTable.innerHTML = '<div class="chart-empty">Пока мало данных для поиска триггеров.</div>';
        return;
      }

      const rows = results.map(result => {
        const best = result.best;
        const lagText = best.lag === 0 ? "тот же день" : `+${best.lag} дн.`;
        const diffClass = best.diff === null ? "" : best.diff >= 0 ? "trigger-high" : "trigger-low";
        const diffText = best.diff === null ? "мало данных" : `${best.diff >= 0 ? "+" : ""}${best.diff.toFixed(1)}`;
        const status = best.diff === null
          ? '<span class="badge warn">мало данных</span>'
          : best.diff >= 1
            ? '<span class="badge pain">проверить</span>'
            : best.diff <= -1
              ? '<span class="badge fasting">ниже</span>'
              : '<span class="badge">слабый сигнал</span>';
        return `
          <tr>
            <td><strong>${escapeHtml(result.name)}</strong></td>
            <td>${escapeHtml(result.occurrences)}</td>
            <td>${escapeHtml(lagText)}</td>
            <td>${formatNumber(best.exposedAvg)} <span class="muted">(${escapeHtml(best.exposedCount)})</span></td>
            <td>${formatNumber(best.baselineAvg)} <span class="muted">(${escapeHtml(best.baselineCount)})</span></td>
            <td class="${diffClass}">${escapeHtml(diffText)}</td>
            <td>${status}</td>
          </tr>
        `;
      }).join("");

      els.triggerTable.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>Группа</th>
              <th>Дней с продуктом</th>
              <th>Лаг</th>
              <th>Боль после</th>
              <th>Боль без</th>
              <th>Разница</th>
              <th>Сигнал</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      `;
    }

    function renderEntry(entry) {
      const typeName = typeNames[entry.type] || entry.type;
      const body = entry.type === "daily_checkin" && Object.keys(entry.answers || {}).length
        ? renderCheckin(entry)
        : `<div class="summary">${escapeHtml(entry.summary || entry.raw_text || "Без текста")}</div>`;
      return `
        <article class="entry">
          <div class="entry-head">
            <div>
              <div class="entry-type">${escapeHtml(typeName)}</div>
              <div class="muted">${escapeHtml(entry.diary_date)}</div>
            </div>
            <div class="entry-time">${escapeHtml(entry.time)}</div>
          </div>
          ${body}
        </article>
      `;
    }

    function groupByDay(entries) {
      const grouped = new Map();
      entries.forEach(entry => {
        if (!grouped.has(entry.diary_date)) grouped.set(entry.diary_date, []);
        grouped.get(entry.diary_date).push(entry);
      });
      return [...grouped.entries()].sort((a, b) => b[0].localeCompare(a[0]));
    }

    function renderContent() {
      const entries = payload.entries.filter(entryMatches);
      if (!entries.length) {
        els.content.className = "empty";
        els.content.innerHTML = "По выбранным фильтрам записей нет.";
        return;
      }
      els.content.className = "";
      els.content.innerHTML = groupByDay(entries).map(([date, dayEntries]) => `
        <section>
          <h2>${escapeHtml(date)}</h2>
          ${dayEntries.map(renderEntry).join("")}
        </section>
      `).join("");
    }

    function render() {
      renderPainChart();
      renderCheckinTable();
      renderTriggerTable();
      renderDayList();
      renderContent();
    }

    async function load() {
      const response = await fetch("/api/entries");
      payload = await response.json();
      els.totalDays.textContent = payload.total_days;
      els.totalEntries.textContent = payload.total_entries;
      els.updated.textContent = "Обновлено: " + new Date().toLocaleString("ru-RU");
      render();
    }

    els.search.addEventListener("input", renderContent);
    els.typeFilter.addEventListener("change", renderContent);
    els.allDays.addEventListener("click", () => {
      selectedDay = "all";
      render();
    });

    load().catch(error => {
      els.content.className = "empty";
      els.content.textContent = "Не удалось загрузить дневник: " + error;
    });
  </script>
</body>
</html>
"""


class DiaryViewerHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/":
            html_response(self, page_html())
            return
        if parsed.path == "/api/entries":
            json_response(self, build_payload())
            return
        if parsed.path == "/health":
            json_response(self, {"ok": True})
            return

        html_response(self, "<h1>404</h1>", status=404)

    def log_message(self, format: str, *args) -> None:
        return


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local browser viewer for the Telegram diary.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8000, help="Port to bind. Default: 8000")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), DiaryViewerHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Diary viewer is running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping diary viewer.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
