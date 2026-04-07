# Video Station — Tabela Wyników QA (GMKtec NucBox G3 / Intel N100)
# Wypełnij podczas testów. Plik: tests/video_station/nuc_results.md

## Środowisko testowe

| Parametr        | Wartość                          |
|-----------------|----------------------------------|
| Model           | GMKtec NucBox G3                 |
| CPU             | Intel N100 / N95                 |
| RAM             | _____ GB                         |
| Storage         | _____ (NVMe/SATA SSD/HDD)        |
| OS              | EthOS (Debian base)              |
| ffmpeg wersja   | `ffmpeg -version` → _____        |
| Data testu      | _______________                  |
| Tester          | _______________                  |

---

## 1. Tabela Wydajności Transkodowania

| Plik (nazwa)    | Rozdzielczość | Kodek src | Enkoder (HW/SW) | CPU Load avg | GPU Video % | Temp max | TTFF (s) | Uwagi             |
|-----------------|---------------|-----------|-----------------|--------------|-------------|----------|----------|-------------------|
|                 | 1080p         | H.264     |                 |              |             |          |          |                   |
|                 | 1080p         | HEVC      |                 |              |             |          |          |                   |
|                 | 4K            | HEVC 10-bit|                |              |             |          |          |                   |
|                 | 4K            | H.264     |                 |              |             |          |          |                   |
|                 | 720p          | H.264     |                 |              |             |          |          |                   |
|                 | 720p          | HEVC      |                 |              |             |          |          |                   |
|                 | 1080p         | AV1       |                 |              |             |          |          |                   |
|                 | FullHD        | MPEG-2    |                 |              |             |          |          | legacy            |

**Legenda kolumn:**
- **Enkoder**: `h264_vaapi` / `hevc_vaapi` / `libx264` (CPU fallback)
- **CPU Load avg**: średnia z `monitor_nuc.sh` podczas 5 minut oglądania
- **GPU Video %**: z `intel_gpu_top` kolumna "Video"
- **Temp max**: szczyt temperatury podczas sesji
- **TTFF**: czas od POST `/hls/start` do pierwszego segmentu HLS (sekundy)

---

## 2. Rapid Seek Test (5 kliknięć co 1 sekundę)

| Film            | Pozycje seek (%)    | Czas odpowiedzi API (ms) | Ghost FFmpeg po teście | Wynik  |
|-----------------|---------------------|--------------------------|------------------------|--------|
|                 | 15%, 35%, 50%, 70%, 85% |                      | 0 / ??? procs          | ✅/❌  |
|                 | 15%, 35%, 50%, 70%, 85% |                      | 0 / ??? procs          | ✅/❌  |

---

## 3. Test Latencji TTFF (Time to First Frame)

Cel: **< 1.5s** dla H.264 (stream copy), **< 3s** dla HEVC (VAAPI transcode)

| Próba # | Kodek    | TTFF (s) | Enkoder    | Wynik (≤ target) |
|---------|----------|----------|------------|------------------|
| 1       | H.264    |          |            | ✅/❌             |
| 2       | H.264    |          |            | ✅/❌             |
| 3       | H.264    |          |            | ✅/❌             |
| 4       | HEVC 4K  |          |            | ✅/❌             |
| 5       | HEVC 4K  |          |            | ✅/❌             |
| Średnia H.264  | —  |          |            | **cel: <1.5s**   |
| Średnia HEVC   | —  |          |            | **cel: <3.0s**   |

---

## 4. Test Thermal Stability (30 minut)

Film: ______________________ (4K HEVC 10-bit)

| Czas     | Temp CPU | GPU Video % | FFmpeg procs | Throttle? |
|----------|----------|-------------|--------------|-----------|
| 0:00     |          |             |              |           |
| 5:00     |          |             |              |           |
| 10:00    |          |             |              |           |
| 15:00    |          |             |              |           |
| 20:00    |          |             |              |           |
| 25:00    |          |             |              |           |
| 30:00    |          |             |              |           |
| **MAX**  |          |             |              | **N/A**   |
| **AVG**  |          |             |              |           |

**Wynik**: ✅ Brak throttlingu / ⚠️ Throttling ___x (≥90°C)

> Uwaga: N100 TDP = 6W, N95 TDP = 15W. N100 jest bardziej narażony na throttling
> przy długim 4K HEVC transcode bez aktywnego chłodzenia.

---

## 5. Ghost Process / Cleanup Test

| Akcja                              | Oczekiwane         | Wynik  |
|------------------------------------|--------------------|--------|
| Start + Stop 1 sesji               | FFmpeg=0, /tmp czysty | ✅/❌ |
| Start + Stop 3 sesji jednocześnie  | FFmpeg=0, /tmp czysty | ✅/❌ |
| Zamknięcie karty (heartbeat timeout) | FFmpeg killed w ≤60s | ✅/❌ |
| Overflow sesji (4. start)          | Najstarsza sesja ubita | ✅/❌ |

---

## 6. Automated Test Results

Uruchom przed wypełnieniem tabeli:
```bash
ETHOS_USER=<user> ETHOS_PASS=<pass> \
  venv/bin/pytest tests/video_station/test_nuc_qa.py -v -s 2>&1 | tee /tmp/nuc_qa_run.txt
```

| Test Class           | Test Name                           | Status | Czas (s) | Uwagi |
|----------------------|-------------------------------------|--------|----------|-------|
| TestVAAPIInit        | test_render_node_exists             |        |          |       |
| TestVAAPIInit        | test_vaapi_encoder_available        |        |          |       |
| TestVAAPIInit        | test_vaapi_device_init_succeeds     |        |          |       |
| TestVAAPIInit        | test_encoder_info_endpoint_shows_vaapi |     |          |       |
| TestVAAPIInit        | test_hevc_vaapi_available           |        |          |       |
| TestTimeToFirstFrame | test_ttff_standard_h264             |        |          |       |
| TestTimeToFirstFrame | test_ttff_hevc_4k                   |        |          |       |
| TestTimeToFirstFrame | test_ttff_repeated_5x               |        |          |       |
| TestRapidSeeking     | test_rapid_seek_kills_previous_ffmpeg |      |          |       |
| TestRapidSeeking     | test_seek_response_under_500ms      |        |          |       |
| TestCleanup          | test_stop_cleans_tmp                |        |          |       |
| TestCleanup          | test_no_ffmpeg_after_all_stops      |        |          |       |

Stress (oddzielnie):
```bash
ETHOS_USER=<user> ETHOS_PASS=<pass> \
  venv/bin/pytest tests/video_station/test_nuc_qa.py -v -s -k stress --timeout=2000
```

| Test Class       | Test Name                       | Status | Throttle events | Temp max |
|------------------|---------------------------------|--------|-----------------|----------|
| TestThermalStress | test_30min_thermal_stability   |        |                 |          |

---

## 7. Rekomendacje dla N100 (wypełnić po testach)

- [ ] Thermal: czy potrzebny dodatkowy wentylator / pad termiczny?
- [ ] `_HLS_MAX_SESSIONS`: zmienić z 3 na _____ (zależy od wyników temp)
- [ ] FFmpeg preset: zostawić `fast` czy zmienić na `superfast`?
- [ ] Segment HLS (`hls_time`): zostawić 4s czy zmienić?
- [ ] Throttle buffer (`_HLS_THROTTLE_AHEAD`): 90s ok, czy zmniejszyć?
