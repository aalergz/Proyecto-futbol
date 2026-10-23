// node --test test/   (no necesita dependencias)
import test from "node:test";
import assert from "node:assert/strict";
import worker, { refrescar, intervalo, cuota } from "../src/index.js";

function kv() { const m = new Map(); return { get: async k => m.get(k) ?? null, put: async (k, v) => { m.set(k, v); }, m }; }
const partido = (id, min, gl, gv, st = "2H") => ({
  fixture: { id, date: "2026-10-25T14:15:00+00:00", status: { short: st, elapsed: min, extra: null } },
  league: { id: 140, name: "La Liga" },
  teams: { home: { id: 1, name: "Barcelona" }, away: { id: 2, name: "Real Madrid" } },
  goals: { home: gl, away: gv },
  events: [{ type: "Card", detail: "Red Card", team: { id: 2 } }],
});

function fakeFetch(respuestas, llamadas) {
  return async (url, opts = {}) => {
    llamadas.push({ url: String(url), opts });
    if (String(url).includes("api.github.com")) return new Response(null, { status: 204 });
    const r = respuestas.shift();
    return new Response(JSON.stringify({ errors: [], response: r }), { headers: { "x-ratelimit-requests-remaining": "40" } });
  };
}
const ENV = (extra = {}) => ({ PIZARRA_KV: kv(), API_FOOTBALL_KEY: "k", LIGAS: "39,140", CUOTA_DIARIA: "50", TTL_MIN: "60", ...extra });

test("convierte partidos en vivo y usa la caché", async () => {
  const llamadas = []; globalThis.fetch = fakeFetch([[partido(10, 63, 1, 0)]], llamadas);
  const env = ENV(); const t0 = Date.UTC(2026, 9, 25, 15, 0);
  const r1 = await refrescar(env, null, t0);
  assert.equal(r1.desde_cache, false);
  assert.equal(llamadas[0].url, "https://v3.football.api-sports.io/fixtures?live=39-140");
  const p = r1.estado.partidos[0];
  assert.deepEqual([p.fid, p.minuto, p.gl, p.gv, p.rojas_local, p.rojas_visitante], [10, 63, 1, 0, 0, 1]);
  const r2 = await refrescar(env, null, t0 + 30_000);
  assert.equal(r2.desde_cache, true);
  assert.equal(llamadas.length, 1);
});

test("reparte la cuota del día", () => {
  const ahora = Date.UTC(2026, 9, 25, 12, 0);            // faltan 12 h para el reinicio
  const env = { CUOTA_DIARIA: "50", TTL_MIN: "60", TTL_SIN_PARTIDOS: "300" };
  const conPartidos = { usadas: 2, partidos: [{}], restantes_api: null };
  assert.equal(Math.round(intervalo(conPartidos, env, ahora)), 900);   // 43200 s / 48
  const pro = { ...env, CUOTA_DIARIA: "7000" };
  assert.equal(intervalo(conPartidos, pro, ahora), 60);                // con plan Pro, el mínimo
  assert.equal(intervalo({ usadas: 0, partidos: [] }, pro, ahora), 300);
  assert.equal(cuota({ usadas: 5, restantes_api: 3 }, env).restantes, 3);
});

test("avisa a GitHub cuando termina un partido", async () => {
  const llamadas = []; globalThis.fetch = fakeFetch([[partido(10, 88, 2, 1)], []], llamadas);
  const env = ENV({ GITHUB_TOKEN: "t", GITHUB_REPO: "ale/pizarra", TTL_MIN: "60" });
  const t0 = Date.UTC(2026, 9, 25, 16, 0);
  await refrescar(env, null, t0);
  const r = await refrescar(env, null, t0 + 15 * 60_000);   // pasado el intervalo repartido
  const gh = llamadas.find(l => l.url.includes("api.github.com"));
  assert.ok(gh, "debió llamar a GitHub");
  assert.equal(gh.url, "https://api.github.com/repos/ale/pizarra/dispatches");
  assert.deepEqual(JSON.parse(gh.opts.body), { event_type: "partido-terminado", client_payload: { partidos: [10] } });
  assert.deepEqual(r.estado.aviso_pendiente, []);
});

test("responde con CORS y JSON", async () => {
  const llamadas = []; globalThis.fetch = fakeFetch([[partido(11, 20, 0, 0, "1H")]], llamadas);
  const res = await worker.fetch(new Request("https://w.dev/en-vivo", { headers: { Origin: "https://ale.github.io" } }),
    ENV({ ORIGEN_PERMITIDO: "https://ale.github.io" }), { waitUntil() {} });
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), "https://ale.github.io");
  const body = await res.json();
  assert.equal(body.partidos[0].estado, "1H");
  assert.equal(body.cuota.usadas, 1);
});

test("si la API falla, sirve lo último y no se cae", async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ errors: { requests: "límite alcanzado" }, response: [] }));
  const env = ENV(); const r = await refrescar(env, null, Date.UTC(2026, 9, 25, 18, 0));
  assert.match(r.estado.error, /límite/);
  assert.deepEqual(r.estado.partidos, []);
});
