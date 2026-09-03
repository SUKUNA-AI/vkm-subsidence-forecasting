# Gate C1: протокол пятиseedового compact sequence temporal screen

## Статус и научная граница

Gate C1 является внутренним исследованием только на 911 строках замороженного
`t1_v1/train`. Этап не открывает исторический validation, уже раскрытый test или
будущий внешний holdout. Результат C1 не является финальной оценкой качества и
не изменяет suite v4 либо holdout policy v3.

Gate C1 допускает ровно четыре архитектуры из неизменяемого Gate C0 registry:

1. `C01_compact_gru`;
2. `C02_compact_lstm`;
3. `C03_causal_tcn`;
4. `C04_probabilistic_gru_student_t`.

Все четыре модели проходят только nested temporal screen. Пространственная,
transition-специфичная и conformal проверка, а также создание suite v5 относятся
к отдельному Gate C2.

## Данные и причинная последовательность

Worker принимает только C0 sequence artifacts и отдельный staging-файл с
целями из outer-train. Вход содержит пять числовых каналов,
`current_campaign_type`, три структурные маски и не содержит идентификаторов в
тензоре. Последовательность заканчивается текущей датой origin; целевое и все
будущие наблюдения исключены контрактом C0.

Числовая импутация и стандартизация, словарь категории и target scaler
обучаются только по текущей train-role. Padding исключён из fit и обнуляется
после transform. Неизвестная категория получает отдельный unknown one-hot
bucket. `sample_id`, `point_id`, `profile_id`, zone/campaign identifiers остаются
только metadata.

## Nested rolling-origin design

Используются 11 замороженных rolling-origin outer folds и последние три
forward-only inner folds каждого outer train. Все пять seeds
`42117, 42118, 42119, 42120, 42121` участвуют как в tuning, так и в outer refit;
выбор лучшего seed запрещён.

Для C01-C03 конфигурация выбирается по pooled row-level inner MAE. Для C04
используется pooled inner CRPS, а inner MAE является первым tie-breaker. Общие
tie-breakers: parameter count и canonical parameter JSON. Epoch count outer
refit равен медиане 15 inner best epochs выбранной конфигурации.

33 логических inner-контекста образуют 13 уникальных train/validation manifest
pairs. Физический cache разрешён только при полном совпадении model, parameters,
seed, sample, sequence, target, preprocessing, code и environment hashes. В
machine inventory сохраняются все 9 240 логических оценок и происхождение каждого
из 3 640 ожидаемых физических fit.

## Архитектуры и objective

- GRU и LSTM получают валидные токены в исходном хронологическом порядке через
  один векторизованный CUDA gather. Затем выполняется dense однонаправленный
  recurrent-проход, а представление берётся на последнем валидном timestep.
  Это математически эквивалентно прежнему packing для используемой
  однонаправленной сети, но не создаёт Python-цикл и CPU/CUDA synchronization в
  каждом minibatch; последний hidden state проходит frozen dropout и linear
  head.
- TCN использует два residual causal Conv1d blocks с dilations 1 и 2; padded
  activations повторно маскируются после каждого блока.
- Point-модели оптимизируют Huber loss с delta 1.0 в стандартизованных единицах,
  а early stopping выполняется по inner MAE.
- Student-t GRU оптимизирует NLL и возвращает `loc`,
  `softplus(raw_scale)+1e-3`, `2.01+softplus(raw_df)`. Point prediction равен
  `loc`; CRPS аппроксимируется фиксированной quantile grid 0.01...0.99.

Canonical point prediction каждой deep-модели является средним пяти fixed-seed
predictions. Параметры Student-t и quantiles не усредняются в фиктивное единое
распределение: native diagnostics публикуются по отдельным seeds.

## Outer-label isolation

Frozen job manifest не содержит пути или аргументы для validation, test,
holdout либо outer labels. Launcher формирует в `work/` минимальный файл целей
только для outer-train. Worker записывает unlabeled shard, в котором запрещены
`y_true`, `observed_rate_mm_y` и любые target values. Независимый scorer получает
доступ к outer labels только после проверки schema и фиксации SHA-256 всех 44
shards; событие фиксируется в outer-label access ledger.

## Воспроизводимость и среда

Authority-среда `work/environments/gate_c_torch` создаётся заново из
неизменяемого `requirements/gate_c_torch.lock.txt`. Основной прогон выполняется
на RTX 5070 Ti с PyTorch 2.13.0+cu130, deterministic algorithms, выключенными
TF32, cuDNN benchmark и mixed precision, `num_workers=0`, batch size 32.
Сетевой доступ разрешён только для staging wheels; runtime socket guard закрывает
сеть во время fit/predict.

CUDA execution использует fused AdamW, если capability подтверждена preflight,
считает early-stopping metric непосредственно на GPU и применяет
`torch.inference_mode()` для prediction. `torch.compile` заранее выключен:
компиляция тысяч короткоживущих малых моделей меняла бы профиль исполнения и
давала бы больший overhead, чем полезную работу. Размер minibatch, objective,
порядок seed policy, grids и admission criteria не изменены. Полное насыщение
RTX 5070 Ti не является научной целью и не обещается: при 911 origins,
последовательностях длиной не более 16 и моделях до 100 000 параметров workload
остаётся принципиально малым.

## Checkpoint policy

Каждый физический fit имеет отдельный SHA-256 `fit_id` и work-only каталог.
После каждых 50 завершённых эпох и в terminal state атомарно перезаписывается
recovery checkpoint с model, optimizer, shuffle-generator и CPU/CUDA RNG state.
Это является одной восстановительной стадией и позволяет продолжить прерванный
fit с той же детерминированной траектории.

Для inner-fit в памяти GPU ранжируются пять лучших полных training states по
той же frozen early-stopping metric, которая уже была задана протоколом: MAE для
C01-C03 и NLL для C04; tie-breaker — меньший номер эпохи. После fit ровно пять
лучших состояний (либо все состояния, если выполнено менее пяти эпох) сохраняются
под `work/gate_c1/checkpoints`, rank 1 повторно загружается для prediction, а
manifest фиксирует пути и SHA-256. Для outer-refit outer labels недоступны:
хранятся пять последних epoch states и всегда выбирается заранее рассчитанная
финальная эпоха. Выбор outer checkpoint по outer target запрещён.

Checkpoint binaries никогда не переносятся в `artifacts/` и не попадают в Git.
В machine artifacts публикуется только проверяемый inventory с относительными
путями, hashes, ranking/selection policy и доказательством
`outer_labels_used_for_ranking=false`. Отсутствующий либо изменённый manifest
инвалидирует соответствующий cache record или outer job.

Каждая конфигурация обязана иметь не более 100 000 параметров. Preflight
сохраняет exact pip freeze, установочные URLs/hashes, hardware capture,
fit/predict/serialization smoke и two-run determinism с tolerance `1e-6`.

Каждый outer job исполняется в отдельном дочернем процессе. После атомарной
записи shard, tuning inventory, selected inner OOF и terminal status дочерний
процесс сбрасывает stdout/stderr и пытается завершить process-global runtime
без CUDA destructor teardown. На Windows/PyTorch полный RNN job может после
этого возвращать `0xC0000409`, хотя минимальные CUDA smoke и normal/hard exit
возвращают ноль. Parent имеет право классифицировать этот единственный код как
post-commit teardown anomaly только после повторной проверки job identity,
трёх file hashes, exact пятиseedового sample universe, prediction schema,
запрещённых target columns и code/config/environment hashes. Проверка и код
выхода записываются в отдельный process-exit ledger. Любой иной ненулевой код,
missing artifact или несовпавшая проверка остаётся protocol failure.

## Temporal admission

Статус `PASSED_TEMPORAL_SCREEN` присваивается только при одновременном выполнении
всех условий:

- завершены 11 outer folds и все пять seeds;
- каждый seed покрывает точный expected sample-ID universe без дублей;
- predictions конечны;
- ensemble pooled и median-fold MAE не хуже B1 более чем на 10%;
- ни один fold не превышает B1 MAE более чем вдвое;
- model-level convergence, preprocessing, environment и leakage checks прошли.

Seed IQR <= 0.50 мм/год и CV <= 10% в C1 являются описательными показателями,
но не post-hoc admission criteria. Низкое качество имеет статус
`REJECTED_TEMPORAL_SCREEN`; полностью учтённая численная несостоятельность модели
— `REJECTED_MODEL_EXECUTION`; нарушение схемы, hashes, границы данных или
полноты — `FAIL_PROTOCOL`. Пустой admission list допустим, а общий корректный
статус этапа остаётся `PASS_C1_TEMPORAL_SCREEN`.

## Неизменяемость

До первого prediction freeze фиксирует SHA-256 C0, B5/B6, suite v4, holdout v3,
configuration и execution sources. Изменение C1 adapter/training/config source
после появления prediction делает затронутые shards недействительными и требует
полного пересчёта с новым code/config hash. Patch отдельных результатов
запрещён.

Изначальный незавершённый прогон C1 был остановлен до outer-label scoring после
8 из 44 outer jobs и 820 из 3640 физических inner fits. По запросу на
checkpoint/CUDA revision его shards и cache перенесены в recoverable quarantine
под `work/`; они не смешиваются с новым authority run. Новая версия протокола
получает новые config/code/environment hashes и запускается с нуля.
