# robot-control

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
./deploy.sh        # rsync в jetson:/home/jetson/robot-control + uv venv (Python 3.14) + aiohttp
```

## Запуск

Нужен root (`/dev/mem`, `/dev/gpiochip0`, pwm sysfs). Системный `python3` на Jetson — 3.6,
поэтому запускать надо python из venv:

```bash
cd ~/robot-control
sudo .venv/bin/python app.py              # hardware mode
sudo .venv/bin/python app.py --dry-run    # PWM не трогается, в консоль: WOULD SET PWM: 1500 1500
```

В фоне (переживёт отключение ssh), pid в `/run/robot-control.pid`, лог `/tmp/robot-control.log`:

```bash
sudo ~/robot-control/run.sh            # доп. аргументы передаются в app.py, напр. --dry-run
sudo ~/robot-control/stop.sh
tail -f /tmp/robot-control.log
```

Интерфейс: **http://192.168.40.247:8080** (или `http://<jetson-ip>:8080`).

## Остановка

- Ctrl+C в консоли, либо `sudo ~/robot-control/stop.sh` (SIGTERM, через 5 с — SIGKILL).
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
  В RC_ARMED любой канал LOST (>100 ms) или INVALID → FAULT, 1500/1500.
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

Дополнительно: канал INVALID только после 3 невалидных импульсов подряд; на выходе гистерезис
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

## ИИ-заезд без лидара и парктроников

`ai_pilot.py` получает команды `robot-vision` из NATS subject
`robot.vision.localization` и использует существующий режим `WEB_ARMED`.
Дополнительных датчиков расстояния этот путь не читает. Без них обнаружения
препятствий нет: полигон должен быть свободным, а оператор — иметь доступ к
физическому отключению питания приводов.

Перед запуском скопировать `ai-config.example.json` в `ai-config.json` и явно
задать четыре поля с `null`: какой выход ведёт левую/правую сторону 4WD и
какой знак PWM вращает каждую сторону вперёд. Пока эти поля не заданы,
`ai_pilot.py` не запустится. Начальные `drive_delta_us=50` и
`turn_delta_us=50` — лишь осторожные стартовые значения для проверки
на вывешенных колёсах; их надо откалибровать на машине. Для левого поворота
левая сторона замедляется, правая ускоряется; правый поворот зеркален.
Команды заднего хода пока дают нейтраль, поскольку прежняя логика разворота
`robot-vision` рассчитана на другую кинематику.

```bash
cp ai-config.example.json ai-config.json
# отредактировать ai-config.json после проверки OUT1/OUT2
sudo ./run.sh
.venv/bin/python ai_pilot.py --config ai-config.json --nats-url nats://<nats-host>:4222
# Эта команда только проверяет получение свежей локализации и показывает PWM.
.venv/bin/python ai_pilot.py --config ai-config.json --nats-url nats://<nats-host>:4222 --arm
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
проверки `ts`. Веб-кнопки `ARM AI` пока нет: запуск через CLI.

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
