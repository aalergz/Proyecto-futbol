# Pizarra Poisson · motor de ratings y datos en vivo

Backend que alimenta la web **Pizarra Poisson**:

- **Motor en Python.** Descarga resultados, ajusta la fuerza de cada equipo con Dixon-Coles, mide su precisión fuera de muestra y genera `ratings.json`.
- **GitHub Actions.** Corre el motor cada 2 horas y publica la web en GitHub Pages.
- **Worker de Cloudflare** (opcional). Sirve los marcadores en vivo y avisa a GitHub cuando termina un partido, para reajustar al momento.

```
API-Football ──► motor (Python, cada 2 h o al terminar un partido) ──► ratings.json ──┐
     │                                                                                ├──► web (GitHub Pages)
     └──────► Worker de Cloudflare (marcadores en vivo, cada 1–30 min) ───────────────┘
```

## Ruta rápida para dejarlo en producción

Se hace una sola vez. Unos 30 minutos.

**1. Clave de API-Football**
Crea una cuenta gratis en api-sports.io y copia tu clave. El plan gratuito permite 100 solicitudes al día.

**2. Repositorio y web**
1. Crea un repositorio en GitHub y sube esta carpeta.
2. En **Settings → Secrets and variables → Actions**, crea el secreto `API_FOOTBALL_KEY` con tu clave.
3. En **Settings → Pages**, elige **Source: GitHub Actions**.
4. En **Actions**, abre *Actualizar ratings y publicar* y pulsa **Run workflow**.

La web queda en `https://<tu-usuario>.github.io/<repo>/` y se actualiza sola cada 2 horas. Con esto ya tienes el **nivel 1**: resultados y ratings con pocas horas de retraso, incluida la Liga Promerica.

**3. Marcadores en vivo (opcional)**
Sigue los pasos de [Worker en vivo](#worker-en-vivo). Al final pones la URL del Worker en `config.toml` → `[web] en_vivo_url` y subes el cambio.

## Instalación local

Requiere Python 3.11 o superior.

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest -q                    # 12 pruebas
export API_FOOTBALL_KEY="tu-clave"     # Windows: set API_FOOTBALL_KEY=tu-clave
python cli.py actualizar
python -m http.server 8000 --directory web     # abre http://localhost:8000
```

`python cli.py probar-api` comprueba tu clave y te dice qué temporadas entrega tu plan, liga por liga. Gasta unas 4 solicitudes. GitHub también lo ejecuta cada vez que corres el flujo a mano.

Sin clave también funciona: usa football-data.co.uk u openfootball y deja la Liga Promerica con ratings de ejemplo.

## Comandos

```bash
python cli.py actualizar                     # todas las ligas: descarga, ajusta, valida, exporta
python cli.py actualizar --ligas crc         # solo una liga; las demás conservan su versión anterior
python cli.py actualizar --sin-validar       # más rápido, sin backtest
python cli.py predecir esp Barcelona "Real Madrid"
python cli.py validar eng --desde 2025-08-01 --csv validacion_eng.csv
python cli.py buscar-liga --pais Costa-Rica  # lista ligas e ids de API-Football
```

Si nada cambió desde la última corrida, `actualizar` no reescribe `ratings.json` y GitHub no vuelve a publicar la web.

## Fuentes de datos

Cada liga prueba sus fuentes en el orden de `config.toml` y usa la primera que responda. Si una se cae, usa la última copia guardada en `datos/cache/`.

| Fuente | Clave | Retraso típico | Notas |
|---|---|---|---|
| `apifootball` (api-sports.io) | Sí | Minutos | Fuente principal. Cubre la Liga Promerica. Devuelve la cuota restante del día en cada respuesta. |
| `footballdata` (football-data.co.uk) | No | 2–4 días | Trae cuotas de cierre: la validación compara el modelo con las casas y simula apuestas. |
| `openfootball` (GitHub) | No | Semanas | La mantienen voluntarios. |
| `csv` | No | El tuyo | Archivo propio (`datos/crc_plantilla.csv` explica el formato). |

**Detección automática.** Con `apifootball = "auto"` el motor busca la liga por país y nombre. Con `temporada_actual = "auto"` toma la temporada que la API marca como vigente, así no hay que cambiar el año cada temporada. Ambas consultas se guardan una semana.

**Cuota de API-Football (plan gratuito: 100 al día)**

| Quién | Consumo |
|---|---|
| Motor, temporadas anteriores | Se bajan una vez y se guardan un mes |
| Motor, temporada actual | 1 solicitud por liga y corrida → 3 ligas × 12 corridas ≈ 36 al día |
| Worker en vivo | El resto. En `wrangler.toml`, `CUOTA_DIARIA = "50"` deja margen |

Si comparten clave y la API informa que queda menos cuota, el Worker obedece a la API.

## Worker en vivo

El Worker (`worker/`) hace tres cosas:

- **Guarda la clave.** La clave queda en el Worker y nunca viaja en la página.
- **Comparte la consulta.** Hace una sola consulta a la API para todos los visitantes.
- **Reparte la cuota.** Consulta cada `restante_del_día ÷ cuota_que_queda`, con un mínimo de 60 segundos.
  - Plan gratuito: cada 15–30 minutos con partidos en juego, y tres veces menos cuando no hay ninguno.
  - Plan Pro (19 USD al mes, 7 500 solicitudes diarias): cada minuto.
  - Para pasar al Pro, solo sube `CUOTA_DIARIA`.

Cuando un partido deja de estar en juego, el Worker dispara el flujo de GitHub, que reajusta los ratings y publica en uno o dos minutos. Si terminan varios partidos juntos, los agrupa en un solo aviso.

### Desplegarlo

Necesitas Node.js 18+ y una cuenta gratis de Cloudflare.

```bash
cd worker
npm install
npx wrangler login
npx wrangler kv namespace create PIZARRA_KV     # pega el id en wrangler.toml
npx wrangler secret put API_FOOTBALL_KEY
npm test                                         # 5 pruebas del Worker
npx wrangler deploy                              # muestra la URL: https://pizarra-en-vivo.<tu-cuenta>.workers.dev
```

Ajusta `worker/wrangler.toml`:

- `LIGAS`: ids separados por coma. Premier es 39 y LaLiga 140; el de la Liga Promerica lo da `python cli.py buscar-liga --pais Costa-Rica`.
- `ORIGEN_PERMITIDO`: tu sitio, por ejemplo `https://tu-usuario.github.io`.
- `CUOTA_DIARIA`: según tu plan.

**Aviso de partido terminado (nivel 3)**

1. En GitHub, crea un token *fine-grained* con acceso solo a tu repositorio y el permiso **Contents: Read and write**.
2. `npx wrangler secret put GITHUB_TOKEN`
3. En `wrangler.toml`, pon `GITHUB_REPO = "tu-usuario/tu-repo"` y vuelve a desplegar.

Si quieres que detecte partidos terminados aunque nadie tenga la web abierta, activa el bloque `[triggers]`. Gasta cuota con el mismo reparto.

**Conectarlo a la web.** En `config.toml`, pon `[web] en_vivo_url = "https://pizarra-en-vivo.<tu-cuenta>.workers.dev"` y sube el cambio. La web muestra:

- **Sección En vivo** en *Próximos partidos*: minuto, marcador, tarjetas rojas y probabilidades antes y ahora.
- **Banda en vivo en el analizador**, cuando el partido elegido se está jugando.

Para revisar la cuota: `https://pizarra-en-vivo.<tu-cuenta>.workers.dev/estado`.

## El modelo

```
log λ_local     = c + h + ataque[local]     + defensa[visitante]
log λ_visitante = c     + ataque[visitante] + defensa[local]
```

- **Máxima verosimilitud** con gradiente analítico (L-BFGS-B). Un ajuste tarda menos de un segundo.
- **Corrección Dixon-Coles (ρ)** para 0–0, 1–0, 0–1 y 1–1, estimada por liga.
- **Decaimiento temporal** `xi = 0.0019` por día: un partido de hace un año pesa cerca de la mitad.
- **Regularización** `0.1`: frena a equipos con pocos partidos. Un ascendido parte de ataque ×0.86 y defensa ×1.16.
- **Elección de parámetros:** `xi` y `regularizacion` se eligieron por log-loss fuera de muestra en Premier y LaLiga 2025-26.

**En vivo.** Los goles esperados se escalan por la parte del partido que falta, con el 55% de los goles en la segunda mitad, y se suman al marcador actual. Una roja baja un 30% los goles del equipo que la recibe y sube un 20% los del rival.

## Precisión medida

Backtest semanal del último año: cada semana se reajusta solo con partidos anteriores y se predicen los de esa semana. Se mide con el Brier score (menor es mejor) contra una línea base con las frecuencias de la liga.

| Liga | Partidos | 1X2 | Más de 2.5 | Ambos anotan | Acierto del favorito |
|---|---|---|---|---|---|
| Premier League | 385 | **+5.7%** | −3.8% | −1.0% | 46.8% |
| LaLiga | 399 | **+8.2%** | +4.5% | −1.2% | 51.9% |

Datos de openfootball hasta el 20 de septiembre de 2026. El modelo mejora con claridad el 1X2. En totales de goles y ambos anotan todavía no supera a la media de la liga. La pestaña *Precisión* de la web se recalcula en cada actualización.

## Estructura

```
cli.py                    comandos
config.toml               ligas, fuentes, parámetros y URL del Worker
motor/fuentes.py          descarga, caché y normalización de resultados
motor/modelo.py           Dixon-Coles: ajuste y probabilidades
motor/validacion.py       backtest fuera de muestra
motor/exportar.py         ratings.json
web/index.html            la Pizarra
worker/                   Worker de Cloudflare (en vivo + aviso a GitHub)
tests/                    pruebas del motor (incluye API-Football simulada)
.github/workflows/        actualización cada 2 h, al terminar partidos y al cambiar la web
```

## Límites

- No modela córners, tarjetas (salvo rojas en vivo) ni remates.
- El simulador no conoce fases finales ni criterios de desempate oficiales.
- Las probabilidades en vivo no ven el dominio del partido (remates, posesión), solo minuto, marcador y expulsiones.

**Juego responsable · +18.** Son estimaciones estadísticas, no garantías.
