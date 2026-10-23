/**
 * Worker de la Pizarra Poisson: marcadores en vivo y aviso de partido terminado.
 *
 *   GET /en-vivo   partidos en juego de las ligas configuradas
 *   GET /estado    cuota usada, última consulta, próximos avisos
 *
 * Por qué existe:
 *  - La clave de API-Football queda aquí, nunca en la página.
 *  - Guarda la última respuesta: mil visitantes = una sola consulta a la API.
 *  - Reparte la cuota diaria: con el plan gratuito consulta cada pocos minutos,
 *    con un plan de pago baja solo hasta TTL_MIN segundos.
 *  - Cuando un partido termina, avisa a GitHub para reajustar los ratings.
 */

const API = "https://v3.football.api-sports.io";
const EN_JUEGO = new Set(["1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"]);
const CLAVE_KV = "estado";

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const cors = corsHeaders(env, request);
    if (request.method === "OPTIONS") return new Response(null, { status: 204, headers: cors });
    if (request.method !== "GET") return json({ error: "Solo se admite GET" }, 405, cors);

    if (url.pathname === "/en-vivo") {
      const r = await refrescar(env, ctx);
      return json(publico(r.estado, r.desde_cache, env), 200, {
        ...cors,
        "Cache-Control": `public, max-age=${Math.min(30, r.siguiente_en_s)}`,
      });
    }
    if (url.pathname === "/estado") {
      const e = await leer(env);
      const c = cuota(e, env);
      return json({ ...c, ultima_consulta: e.ts ? new Date(e.ts).toISOString() : null,
        partidos_en_juego: e.partidos.length, aviso_pendiente: e.aviso_pendiente,
        ultimo_aviso: e.ultimo_aviso ? new Date(e.ultimo_aviso).toISOString() : null,
        siguiente_en_s: intervalo(e, env) }, 200, cors);
    }
    return json({ servicio: "Pizarra Poisson · en vivo", rutas: ["/en-vivo", "/estado"] }, 200, cors);
  },

  // Opcional: si activas un cron en wrangler.toml, detecta partidos terminados
  // aunque nadie tenga la web abierta. Respeta el mismo reparto de cuota.
  async scheduled(event, env, ctx) {
    await refrescar(env, ctx);
  },
};

// --------------------------------------------------------------------------- //
export async function refrescar(env, ctx, ahora = Date.now()) {
  let e = await leer(env);
  const dia = new Date(ahora).toISOString().slice(0, 10);       // la cuota se reinicia a las 00:00 UTC
  if (e.dia !== dia) e = { ...e, dia, usadas: 0, restantes_api: null };

  const espera = intervalo(e, env, ahora);
  const edad = (ahora - (e.ts || 0)) / 1000;
  const c = cuota(e, env);
  if (edad < espera || c.restantes <= 0) {
    const pendientes = (e.aviso_pendiente || []).length;
    await quizasAvisar(e, env, ahora);
    if ((e.aviso_pendiente || []).length !== pendientes) await guardar(env, e, ctx);
    return { estado: e, desde_cache: true, siguiente_en_s: Math.max(1, Math.round(espera - edad)) };
  }

  try {
    const ligas = String(env.LIGAS || "").split(",").map(s => s.trim()).filter(Boolean);
    const live = ligas.length ? ligas.join("-") : "all";
    const res = await fetch(`${API}/fixtures?live=${live}`, {
      headers: { "x-apisports-key": env.API_FOOTBALL_KEY || "" },
    });
    const restantes = res.headers.get("x-ratelimit-requests-remaining");
    const data = await res.json();
    e.usadas = (e.usadas || 0) + 1;
    if (restantes !== null && restantes !== "") e.restantes_api = Number(restantes);
    const errores = data.errors && (Array.isArray(data.errors) ? data.errors.length : Object.keys(data.errors).length);
    if (!res.ok || errores) throw new Error(JSON.stringify(data.errors || res.status));

    const partidos = (data.response || []).map(convertir).filter(p => EN_JUEGO.has(p.estado));
    const antes = new Set(e.partidos.map(p => p.fid));
    const ahoraIds = new Set(partidos.map(p => p.fid));
    const terminados = [...antes].filter(id => !ahoraIds.has(id));
    if (terminados.length) e.aviso_pendiente = [...new Set([...(e.aviso_pendiente || []), ...terminados])];

    e = { ...e, ts: ahora, partidos, error: null };
  } catch (err) {
    // Se sigue sirviendo lo último que había; se reintenta en el próximo intervalo
    e = { ...e, ts: ahora, error: String(err.message || err) };
  }
  await quizasAvisar(e, env, ahora);
  await guardar(env, e, ctx);
  return { estado: e, desde_cache: false, siguiente_en_s: Math.round(intervalo(e, env, ahora)) };
}

/** Segundos entre consultas: reparte la cuota que queda en lo que falta del día. */
export function intervalo(e, env, ahora = Date.now()) {
  const minimo = Number(env.TTL_MIN || 60);
  const sinPartidos = Number(env.TTL_SIN_PARTIDOS || 300);
  const { restantes } = cuota(e, env);
  const d = new Date(ahora);
  const finDia = Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate() + 1);
  const reparto = (finDia - ahora) / 1000 / Math.max(restantes, 1);
  // Sin partidos en juego se consulta tres veces menos, para guardar cuota para cuando los haya
  if (!(e.partidos && e.partidos.length)) return Math.max(minimo, sinPartidos, reparto * 3);
  return Math.max(minimo, reparto);
}

export function cuota(e, env) {
  const limite = Number(env.CUOTA_DIARIA || 50);
  let restantes = limite - (e.usadas || 0);
  // Si la API informa menos (la misma clave la usa GitHub Actions), manda la API
  if (e.restantes_api !== null && e.restantes_api !== undefined) restantes = Math.min(restantes, e.restantes_api);
  return { limite, usadas: e.usadas || 0, restantes: Math.max(0, restantes) };
}

async function quizasAvisar(e, env, ahora) {
  if (!e.aviso_pendiente || !e.aviso_pendiente.length) return;
  if (!env.GITHUB_TOKEN || !env.GITHUB_REPO) { e.aviso_pendiente = []; return; }
  const espera = Number(env.AVISO_CADA_MIN || 10) * 60 * 1000;   // agrupa partidos que terminan juntos
  if (e.ultimo_aviso && ahora - e.ultimo_aviso < espera) return;
  try {
    const r = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "User-Agent": "pizarra-poisson-worker",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ event_type: "partido-terminado", client_payload: { partidos: e.aviso_pendiente } }),
    });
    if (r.status === 204 || r.ok) { e.aviso_pendiente = []; e.ultimo_aviso = ahora; }
    else e.error = `GitHub respondió ${r.status}`;
  } catch (err) {
    e.error = `No se pudo avisar a GitHub: ${err.message || err}`;
  }
}

function convertir(f) {
  const ev = Array.isArray(f.events) ? f.events : [];
  const rojas = id => ev.filter(x => x.type === "Card" && /red/i.test(x.detail || "") && x.team && x.team.id === id).length;
  return {
    fid: f.fixture.id,
    liga_id: f.league && f.league.id,
    liga: f.league && f.league.name,
    inicio: f.fixture.date,
    estado: f.fixture.status.short,
    minuto: f.fixture.status.elapsed ?? 0,
    descuento: f.fixture.status.extra ?? null,
    local: f.teams.home.name,
    visitante: f.teams.away.name,
    gl: f.goals.home ?? 0,
    gv: f.goals.away ?? 0,
    rojas_local: ev.length ? rojas(f.teams.home.id) : null,
    rojas_visitante: ev.length ? rojas(f.teams.away.id) : null,
  };
}

function publico(e, desdeCache, env) {
  const c = cuota(e, env);
  return {
    actualizado: e.ts ? new Date(e.ts).toISOString() : null,
    siguiente_en_s: Math.round(intervalo(e, env)),
    desde_cache: desdeCache,
    partidos: e.partidos || [],
    aviso: e.error || null,
    cuota: { usadas: c.usadas, limite: c.limite },
  };
}

async function leer(env) {
  const vacio = { ts: 0, partidos: [], usadas: 0, dia: null, restantes_api: null, aviso_pendiente: [], ultimo_aviso: 0 };
  try {
    const t = await env.PIZARRA_KV.get(CLAVE_KV);
    return t ? { ...vacio, ...JSON.parse(t) } : vacio;
  } catch {
    return vacio;
  }
}

async function guardar(env, e, ctx) {
  const p = env.PIZARRA_KV.put(CLAVE_KV, JSON.stringify(e), { expirationTtl: 60 * 60 * 48 });
  if (ctx && ctx.waitUntil) ctx.waitUntil(p); else await p;
}

function corsHeaders(env, request) {
  const permitido = env.ORIGEN_PERMITIDO || "*";
  const origen = request.headers.get("Origin") || "";
  const lista = permitido.split(",").map(s => s.trim());
  const allow = lista.includes("*") ? "*" : (lista.includes(origen) ? origen : lista[0]);
  return { "Access-Control-Allow-Origin": allow, "Access-Control-Allow-Methods": "GET, OPTIONS", Vary: "Origin" };
}

function json(obj, status, headers) {
  return new Response(JSON.stringify(obj), {
    status, headers: { "Content-Type": "application/json; charset=utf-8", ...headers },
  });
}
