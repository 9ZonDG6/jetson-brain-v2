# Управление v2

Локальное веб-приложение для Jetson Nano: мониторинг штатного RC-пульта HotRC F-06A и
безопасное управление 4WD motor controller через два hardware PWM выхода.

Платформа: Jetson Nano Developer Kit, Tegra210, L4T R32.7.6, Python 3.14 (uv venv).

## Wiring

| Сигнал | Jetson physical pin | Linux | Примечание |
|---|---|---|---|
| F-06A CH1 → | 29 (вход) | gpiochip0 line 149, PS5 | pinmux 0x700031e4 |
| F-06A CH2 → | 31 (вход) | gpiochip0 line 200, PZ0 | pinmux 0x7000327c |
| → controller PWM1 | 32 (выход) | pwmchip0/pwm0, PV0 | pinmux 0x700031fc = 0x45 |
| → controller PWM2 | 33 (выход) | pwmchip0/pwm2, PE6 | pinmux 0x70003248 = 0x46 |
| GND | GND | | общая земля F-06A, Jetson, controller |

+V от F-06A к Jetson **не** подключён. Приёмник питается от своей стороны.

Pinmux для 32/33 приложение выставляет само при старте (runtime, через `/dev/mem`, не сохраняется
после перезагрузки). DTB/extlinux не меняются.

## Установка / деплой

С рабочей машины (из этого каталога):

```bash
./scripts/deploy.sh  # ARM64 wheelhouse, rsync и docker compose up -d
```

На текущем Jetson плагин Compose установлен в `~/.docker/cli-plugins/`.
Для сборки образа Jetson не требуется доступ к PyPI: скрипт переносит готовые
ARM64 Python-пакеты и статический FFmpeg. Основной образ `python:3.14-slim-bookworm`
должен быть заранее загружен в Docker. Рабочие конфигурации, `recordings/` и
`nats-data/` скрипт не удаляет.

## Запуск

Обычный запуск на Jetson — через Docker Compose. Контейнер управления получает
доступ к PWM, GPIO и `/dev/mem`, а NATS слушает localhost:4222 и
192.168.40.247:4222 для `robot-vision` в локальной сети:

```bash
cd ~/jetson-brain-v2
docker compose up -d
docker compose ps
docker compose logs -f control
```

Для запуска вне контейнера нужен root (`/dev/mem`, `/dev/gpiochip0`, pwm sysfs).
Системный `python3` на Jetson — 3.6, поэтому нужен Python 3.14 из venv:

```bash
cd ~/jetson-brain-v2
sudo .venv/bin/python -m jetson_brain_v2.app              # hardware mode
sudo .venv/bin/python -m jetson_brain_v2.app --dry-run    # PWM не трогается
```

В фоне (переживёт отключение ssh), pid в `/run/robot-control.pid`, лог `/tmp/robot-control.log`:

```bash
sudo ~/jetson-brain-v2/scripts/run.sh   # доп. аргументы передаются в app.py, напр. --dry-run
sudo ~/jetson-brain-v2/scripts/stop.sh
tail -f /tmp/robot-control.log
```

Интерфейс: **http://192.168.40.247:8080** (или `http://<jetson-ip>:8080`).

## Остановка

- Ctrl+C в консоли, либо `sudo ~/jetson-brain-v2/scripts/stop.sh` (SIGTERM, через 5 с — SIGKILL).
  Не используйте `pkill -f "...app.py"` из ssh-однострочника: он убивает и собственную shell-команду.
- При остановке: сразу DISARMED и 1500/1500, все браузеры отключаются, выход < 2 с.
- SIGINT / SIGTERM / SIGHUP → выходы 1500/1500, PWM остаётся включённым на нейтрали.
- `kill -9` или падение питания Jetson **не** обрабатываются программой: kernel PWM продолжит
  выдавать последнее значение. Физический аварийный стоп — выключение питания моторов.

## Safety

- Старт backend: OUT1 = OUT2 = 1500 us, режим DISARMED. Ничего не армится автоматически.
- Порядок инициализации: export → period 20 ms → duty 1500 us → enable → только потом pinmux на PWM.
- Состояния: `DISARMED`, `RC_ARMED`, `WEB_ARMED`, `FAULT`. Любое не-armed состояние = 1500/1500.
- **ARM RC** разрешён только если оба канала CONNECTED и стики в центре (±100 us).
  В RC_ARMED потеря сигнала (>100 ms) сразу даёт FAULT. При INVALID выходы
  переходят в нейтраль; если сигнал не восстановился за 250 ms, также FAULT.
- **ARM WEB**: выход остаётся 1500/1500, ползунки сбрасываются в 1500; только последующее движение
  меняет PWM. Управлять может только браузер, нажавший ARM WEB.
  Heartbeat >500 ms нет, закрыта вкладка, оборвался WebSocket → FAULT, 1500/1500.
- **STOP** (кнопка или пробел) → DISARMED, 1500/1500.
- Из FAULT выход только ручным ARM. Автоматического восстановления движения нет.
- Backend clamp'ит всё в 1000..2000 us (мусор/NaN → 1500) внутри `motor_io`, а не в эндпоинтах.
- Safety-цикл 50 Hz работает в отдельном потоке, независимо от веб-сервера; исключение в цикле → FAULT.

### Ограничение Tegra PWM

У Tegra210 PWM 8-битный duty (256 шагов на период). При 20 ms шаг = 78.125 us:
1000 → 1016, 1500 → **1484**, 2000 → 2031 us. Между 1000 и 2000 всего ~14 ступеней.
В UI показывается и запрошенное, и реальное (`real`) значение.

### CPU tuning (точность RC)

На ядре 4.9 timestamp фронта ставится в threaded IRQ, и пробуждение CPU из C7 плюс смена частоты
(`schedutil`) дают огромный джиттер. Замер при стиках в центре (4 с):

| Настройка | разброс p5..p95 | отклонение > 20 us |
|---|---|---|
| schedutil + C7 (по умолчанию) | 1440..1634 us | ~45 % импульсов |
| performance + C7 | — | ~21 % |
| **performance + C7 выключен** | **1488..1500 us** | **0 %** |

Поэтому при старте приложение ставит governor `performance` и отключает cpuidle state1 (C7),
при выходе восстанавливает исходные значения (sysfs, не сохраняется после перезагрузки).
Отключить: `--no-cpu-tuning`. Цена — несколько сотен мВт потребления.

Дополнительно: вход пульта берёт медиану последних 7 корректных импульсов,
чтобы краткие ложные команды не двигали моторы. Канал INVALID только после
3 невалидных импульсов подряд; на выходе гистерезис
±15 us на границах ступеней PWM и зона нейтрали ±25 us (всё в ней → нейтральная ступень).

### RC input

Kernel GPIO line events (`GPIO_GET_LINEEVENT_IOCTL`, uAPI v1, без libgpiod), фронты с kernel timestamp,
ширина = falling − rising, медиана последних 3 импульсов. Без busy-loop и `sleep`.
На ядре 4.9 timestamp ставится в threaded IRQ, поэтому под нагрузкой возможен джиттер в десятки us.
Валидно: 900..2100 us. Канал LOST, если нет валидного импульса >100 ms; INVALID — фронты есть, ширина вне диапазона.

Линии 149/200 не должны быть заняты sysfs (`/sys/class/gpio/gpio149`, `gpio200`) или другим сервисом
(раньше их держал `robot-peripherals` как реле) — иначе `EBUSY`, в UI будет `RC input: ... line busy`.

## API

| Метод | Путь | |
|---|---|---|
| GET | `/api/status` | полное состояние |
| POST | `/api/arm/rc` | 409 + `error`, если нельзя |
| POST | `/api/arm/web` | заголовок `X-Client-Id` или `{"client_id": ...}` |
| POST | `/api/stop` | всегда 1500/1500 |
| POST | `/api/center` | WEB цели = 1500/1500 |
| POST | `/api/web-control` | `{"ch1_us":1520,"ch2_us":1480}`, только от владельца WEB ARM |
| POST | `/api/heartbeat` | |
| GET | `/ws?client_id=...` | статус 20 Hz; клиент шлёт `{"type":"hb"}` и `{"type":"control",...}` |
| POST | `/api/ai/config` | сохранить соответствие сторон из текущего WASD и настройки RC |
| POST | `/api/ai/start`, `/api/ai/stop` | запустить/остановить ИИ-пилот |
| GET | `/api/camera/stream.mjpg` | прямой MJPEG-поток |
| POST | `/api/camera/record/start`, `/api/camera/record/stop` | запись камеры в `recordings/*.avi` |

## ИИ-заезд без лидара и парктроников

ИИ-пилот получает команды `robot-vision` из NATS subject
`robot.vision.localization` и использует существующий режим `WEB_ARMED`.
Дополнительных датчиков расстояния этот путь не читает. Без них обнаружения
препятствий нет: полигон должен быть свободным, а оператор — иметь доступ к
физическому отключению питания приводов.

В админке нужно сначала проверить, что WASD едет в верных направлениях, и
нажать «Сохранить проводку из текущего WASD». Админка возьмёт сохранённые
в браузере галочки swap/инверсии и запишет `config/ai.json` на Jetson.
Это избавляет от угадывания, какая сторона подключена к OUT1. Можно также
вручную скопировать `config/ai.example.json` в `config/ai.json` и явно задать
четыре поля с `null`. Начальные `drive_delta_us=50` и `turn_delta_us=50` —
значения для проверки на вывешенных колёсах; их надо откалибровать на машине.
Для левого поворота
левая сторона замедляется, правая ускоряется; правый поворот зеркален.
Команды заднего хода пока дают нейтраль, поскольку прежняя логика разворота
`robot-vision` рассчитана на другую кинематику.

Пилот сам управляет состоянием `robot-vision` через `robot.vision.control`:
если в админке выбран «Маршрут», перед стартом отправляет `pause` и `set_route` (`robot-vision` меняет маршрут только на паузе; список маршрутов захардкожен в его `module2_localization/config.py` → `ROUTES`, в админке он повторён вручную); после
захвата WEB ARM отправляет `resume` (Pilot в `robot-vision` стартует на паузе и
сам с неё не выходит); при остановке — `pause`. Пустой «Маршрут» оставляет
тот, что уже выбран в `robot-vision`. Сообщения с `paused: true` считаются
признаком живого источника, но всегда дают 1500/1500.

`robot-vision` работает на отдельной машине, поэтому её часы и часы Jetson
должны быть синхронизированы (NTP/chrony): команда старше 0.5 с по полю `ts`
отбрасывается. Причина простоя видна в журнале пилота прямо в админке, например
`producer timestamp 3.50 s old (limit 0.50 s; check clock sync)`.

В админке есть `ARM ИИ` и «Остановить ИИ». Подруливание с пульта можно
включить при сохранении конфигурации: режим «разница двух каналов» подходит
для танкового пульта, «отдельный канал» — для стика руля. ИИ оставляет газ за
собой, а пульт добавляет ограниченный поворот. При потере сигнала RC в этом
режиме ИИ снимает управление. Выбор канала и знака следует проверить при
вывешенных колёсах.

```bash
cp config/ai.example.json config/ai.json
# отредактировать config/ai.json после проверки OUT1/OUT2
sudo ./scripts/run.sh
.venv/bin/jetson-ai-pilot --config config/ai.json --nats-url nats://<nats-host>:4222
# Эта команда только проверяет получение свежей локализации и показывает PWM.
.venv/bin/jetson-ai-pilot --config config/ai.json --nats-url nats://<nats-host>:4222 --route <маршрут> --arm
```

Для сквозной проверки без движения запустить `robot-control` с `--dry-run`,
а пилот — с `--arm --allow-dry-run`. Без последнего флага пилот откажется
армиться при `DRY-RUN`, чтобы режим проверки нельзя было спутать с реальным.

Для движения требуется `--arm`, свежая локализация (`inliers >= 20`, `ts` не
старше 0.5 с), доступный NATS и `robot-control` в `DISARMED`. При `lost`,
`stop`, невалидной локализации или паузе выходы становятся 1500/1500. При
молчании источника дольше 0.5 с процесс снимает ARM; watchdog самого
`robot-control` также снимает WEB ARM, если процесс перестал отправлять
команды. После потери связи самопроизвольного повторного ARM нет. Ctrl+C
отправляет STOP, если ИИ ещё владеет управлением. `ARM RC` вручную передаёт
управление пульту; пилот после этого завершится и не отключит пульт.

Ограничения: алгоритм машинного зрения и его карты остаются в отдельном
`robot-vision`; этот репозиторий содержит только приём его команд и выдачу
управления. Часы `robot-vision` и Jetson должны быть синхронизированы для
проверки `ts`.

## Камера: поток и запись

Для USB-камеры скопировать `config/camera.example.json` в `config/camera.json`,
указать реальное `/dev/videoN`, формат, разрешение и FPS; затем перезапустить
приложение. На Jetson нужен `ffmpeg`. RTSP-камера поддерживается через
`{"kind":"rtsp","url":"rtsp://..."}` с шириной, высотой и FPS. Для CSI
или другого источника предусмотрен `kind: "command"` и массив `command`:
команда должна писать последовательные JPEG-кадры в stdout. Конкретный
GStreamer-конвейер зависит от модели камеры и пока не проверен на Jetson.

Контейнер содержит статический FFmpeg для ARM64. Админка показывает прямой
MJPEG-поток и кнопки начала/остановки записи.
Для USB-камеры с V4L2-фокусировкой админка также переключает автофокус и
ручной фокус. Положение линзы сохраняется в `config/focus.json` и
восстанавливается после перезапуска. Ручной фокус полезно подбирать по
прямому потоку на рабочей дистанции до трассы.
Поток и запись используют один захват камеры. Файлы сохраняются локально в
`recordings/camera-YYYYMMDD-HHMMSS.avi` с MJPEG-видео, пригодным для
последующей обработки; при деплое файлы и конфигурация камеры сохраняются.
Кнопка «Сделать фото» берёт текущий кадр из того же потока и добавляет JPEG
в `recordings/photos.zip`. Админка скачивает или удаляет весь ZIP целиком.
Сначала проверьте свободное место на диске для длительной записи.

## Ручные проверки

### Pin 29/31 (вход RC)

```bash
sudo grep -E 'gpio-(149|200) ' /sys/kernel/debug/gpio     # не должны быть заняты (или robot-control)
curl -s localhost:8080/api/status | python3 -m json.tool  # ch1_us/ch2_us, ch*_hz ≈ 50, ch*_status
```

Без приложения (линии свободны), через sysfs:

```bash
echo 149 | sudo tee /sys/class/gpio/export; cat /sys/class/gpio/gpio149/direction   # должно быть in
watch -n0.2 cat /sys/class/gpio/gpio149/value                                        # 0/1 при сигнале
echo 149 | sudo tee /sys/class/gpio/unexport
```

### Pin 32/33 (выход PWM)

```bash
sudo cat /sys/kernel/debug/pwm | grep -A4 7000a000   # pwm-0 и pwm-2: period 20000000 duty 1500000
sudo busybox devmem 0x700031fc 32                    # 0x45 → pin 32 = PWM0
sudo busybox devmem 0x70003248 32                    # 0x46 → pin 33 = PWM2
```

Осциллограф: щуп на pin 32 (затем 33), земля на GND Jetson. Ожидается 3.3 V, 50 Hz, импульс ≈1.48 ms
в DISARMED. Ручной PWM без приложения (моторы выключены!):

```bash
echo 2 | sudo tee /sys/class/pwm/pwmchip0/export
echo 20000000 | sudo tee /sys/class/pwm/pwmchip0/pwm2/period
echo 1500000  | sudo tee /sys/class/pwm/pwmchip0/pwm2/duty_cycle
echo 1        | sudo tee /sys/class/pwm/pwmchip0/pwm2/enable
sudo busybox devmem 0x70003248 32 0x46
```
