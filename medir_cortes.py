#!/usr/bin/env python3
"""Mide cortes de escena de un video con ffmpeg y devuelve JSON. Solo biblioteca estándar.

Uso en GitHub Actions: lee VIDEO_URL, JOB_ID, CALLBACK_URL y CALLBACK_TOKEN del entorno.
Uso local de prueba:   python3 medir_cortes.py --file video.mp4 --job-id prueba --out salida.json
No imprime nunca la URL del video ni las claves.
"""
import argparse, hashlib, json, os, re, subprocess, sys, tempfile, time
import urllib.error, urllib.parse, urllib.request

HOSTS_PERMITIDOS = (".cdninstagram.com", ".fbcdn.net")
MAX_BYTES = 300 * 1024 * 1024
UMBRAL = 0.20          # candidatos desde este puntaje de escena
FUSION_S = 0.25        # cortes más cercanos que esto se funden en uno
SEGURO, PROBABLE = 0.30, 0.25


def falla(msg, code=2):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def host_ok(url):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        return False
    h = (u.hostname or "").lower()
    return any(h.endswith(s) for s in HOSTS_PERMITIDOS)


class SoloHostsPermitidos(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_ok(newurl):
            raise urllib.error.URLError("redireccion a host no permitido")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def descargar(url, destino):
    if not host_ok(url):
        falla("la URL no es https de cdninstagram.com o fbcdn.net")
    op = urllib.request.build_opener(SoloHostsPermitidos())
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    h = hashlib.sha256()
    total = 0
    try:
        with op.open(req, timeout=60) as r, open(destino, "wb") as f:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                total += len(b)
                if total > MAX_BYTES:
                    falla("el video supera 300 MB")
                h.update(b)
                f.write(b)
    except urllib.error.HTTPError as e:
        falla(f"descarga rechazada: HTTP {e.code}", 3)
    except Exception as e:
        falla(f"descarga fallida: {type(e).__name__}", 3)
    return total, h.hexdigest()


def ffprobe(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
                       capture_output=True, text=True)
    if r.returncode:
        falla("ffprobe no pudo leer el archivo", 4)
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    if not v:
        falla("el archivo no tiene video", 4)
    num, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    return {
        "duracion_s": round(float(d["format"]["duration"]), 3),
        "fps": round(fps, 3),
        "ancho": v.get("width"), "alto": v.get("height"),
        "cuadros": int(v["nb_frames"]) if v.get("nb_frames", "").isdigit() else None,
        "codec_video": v.get("codec_name"), "tiene_audio": a is not None,
    }


def cortes(path, fps):
    filtro = f"select='gt(scene,{UMBRAL})',metadata=print:file=-"
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", path, "-vf", filtro, "-an", "-f", "null", "-"],
                       capture_output=True, text=True)
    if r.returncode:
        falla("ffmpeg fallo al detectar escenas", 5)
    crudos, t = [], None
    for linea in r.stdout.splitlines():
        m = re.search(r"pts_time:([0-9.]+)", linea)
        if m:
            t = float(m.group(1))
        m = re.search(r"lavfi\.scene_score=([0-9.]+)", linea)
        if m and t is not None:
            crudos.append((t, float(m.group(1))))
            t = None
    fund = []
    for t, s in sorted(crudos):
        if fund and t - fund[-1][0] <= FUSION_S:
            if s > fund[-1][1]:
                fund[-1] = (t if s - fund[-1][1] > 0.1 else fund[-1][0], s)
            continue
        fund.append((t, s))
    out = []
    for t, s in fund:
        nivel = "seguro" if s >= SEGURO else "probable" if s >= PROBABLE else "dudoso"
        out.append({"tiempo_s": round(t, 2), "cuadro": round(t * fps), "puntaje": round(s, 2), "nivel": nivel})
    return out


def enviar(resultado):
    url, token = os.environ.get("CALLBACK_URL"), os.environ.get("CALLBACK_TOKEN")
    if not url:
        return False
    cuerpo = json.dumps(resultado).encode()
    for intento in range(1, 4):
        req = urllib.request.Request(url, data=cuerpo, method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                print(f"callback: HTTP {r.status}")
                return True
        except urllib.error.HTTPError as e:
            print(f"callback intento {intento}: HTTP {e.code}")
            if 400 <= e.code < 500 and e.code != 429:
                break
        except Exception as e:
            print(f"callback intento {intento}: {type(e).__name__}")
        time.sleep(3 * intento)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--file")
    ap.add_argument("--job-id", default=os.environ.get("JOB_ID", ""))
    ap.add_argument("--out", default="resultado.json")
    a = ap.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a.job_id or ""):
        falla("job_id invalido (solo letras, numeros, - y _, maximo 64)")
    with tempfile.TemporaryDirectory() as tmp:
        if a.file:
            path = a.file
            tam = os.path.getsize(path)
            sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
        else:
            url = os.environ.get("VIDEO_URL", "")
            path = os.path.join(tmp, "video.mp4")
            tam, sha = descargar(url, path)
        meta = ffprobe(path)
        lista = cortes(path, meta["fps"])
        ver = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout.splitlines()[0]
    resultado = {
        "job_id": a.job_id, "estado": "OK", "tamano_bytes": tam, "sha256": sha, "video": meta,
        "parametros": {"umbral_candidatos": UMBRAL, "fusion_s": FUSION_S, "seguro_desde": SEGURO, "probable_desde": PROBABLE},
        "cortes": lista,
        "resumen": {n: sum(1 for c in lista if c["nivel"] == n) for n in ("seguro", "probable", "dudoso")},
        "ffmpeg": ver, "medido_en": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    json.dump(resultado, open(a.out, "w"), ensure_ascii=False, indent=2)
    print(f"cortes: {resultado['resumen']}  duracion: {meta['duracion_s']} s")
    if not a.file and not enviar(resultado):
        if os.environ.get("CALLBACK_URL"):
            falla("no se pudo entregar el resultado", 6)


if __name__ == "__main__":
    main()
