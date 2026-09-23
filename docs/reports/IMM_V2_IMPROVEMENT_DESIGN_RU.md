# IMM: проект возможных улучшений после data release v2

Статус: **DESIGN ONLY**. Код моделей, гиперпараметры, candidate suite и прошлые
результаты не изменены. Ни один описанный эксперимент здесь не выполнен.

## A. Continuous-time regime transitions

В `src/skru1/imm_kalman.py:262` `_imm_step` вызывает
`_regime_transition_matrix()` без аргумента времени. В строках 439–447 матрица
составляется непосредственно из `p_stable_stay` и `p_transition_stay`.
Таким образом, вероятность переключения за одно наблюдение одинакова при
коротком интервале и длинном пропуске. При этом физическая state transition уже
использует `delta_years`, а retention ускорения в строках 528–536 равен
`retention_per_year ** delta_years`. Ограничение относится именно к вероятностям
режимов; утверждать, что вся модель игнорирует время, было бы неверно.

Для двух состояний S/T и интенсивностей α, β ≥ 0 в год:

```text
Q = [[-α, α], [β, -β]],  P(Δt) = exp(Q Δt)
λ = α + β,  πS = β/λ,  πT = α/λ
PSS = πS + πT exp(-λΔt)     PST = πT(1-exp(-λΔt))
PTS = πS(1-exp(-λΔt))       PTT = πT + πS exp(-λΔt)
```

При λ=0 следует вернуть I. Средние dwell times равны 1/α и 1/β.
Параметры получают понятный смысл в календарном времени. Матрица удовлетворяет
P(0)=I и P(a+b)=P(a)P(b); число зарегистрированных кампаний больше не задаёт
искусственно часы процесса. Общий случай реализуем через
[официальный `scipy.linalg.expm`](https://docs.scipy.org/doc/scipy/reference/generated/scipy.linalg.expm.html).
Основу semigroup/generator формализма излагают
[лекционные материалы IISc](https://ece.iisc.ac.in/~parimal/2025/spqt/lecture-18.pdf).

Для честного первого сравнения можно сопоставить существующую матрицу с Q на
заранее выбранном Δt₀ из **train calendar**, сохранив stationary occupancy и
вероятность сохранения режима именно при Δt₀. Для a=1-pSS, b=1-pTT и
0<a+b<1: λ=-log(1-a-b)/Δt₀; α=λa/(a+b); β=λb/(a+b).
Это не оценка геологической интенсивности по старым лучшим результатам.
Если исходная матрица не допускает такое двухсостоянийное embedding, нельзя
просто взять её matrix log и объявить результат корректным Q.

Затем разрешить маленькую train-only grid по двум dwell times, с тем же числом
проверок, что у дискретного варианта. Не увеличивать одновременно размер q-grid.
qStable/qTransition, observation model, история, initialization и calibration
должны совпадать. Отдельные ablations: fixed-step P; согласованный Q без новой
настройки; Q с тем же бюджетом настройки; контроль на регулярной и нерегулярной
выборке одних latent worlds. Отчёт: ошибки next-planned rate, likelihood,
calibration, false switches на год и после gap, чувствительность к dwell times.

Матрица P(Δt) описывает смену режима между наблюдениями, но сама по себе не
интегрирует все возможные изменения непрерывной динамики внутри длинного gap.
Текущий IMM mixing остаётся приближением. Forecast в строках 203–223 сейчас
смешивает прогнозы с origin probabilities; отдельная ablation должна оценить
распространение вероятностей и состояния через forecast horizon. Нельзя
одновременно менять этот прогноз и объявлять выигрыш следствием только Q.

## B. Derived rate / acceleration assimilation

В `imm_kalman.py:298–364` выполняются три последовательных scalar updates:
position, rate, acceleration; их log likelihood складываются. Variance rate
учитывает σ текущей/предыдущей позиции и Δt, но cross-covariance не передаётся.
Variance acceleration основана на train scale и множителе. В новом генераторе
`scenario_frames` rate и acceleration вычислены из тех же settlement samples.
Это не три независимых датчика: допущение независимости может излишне уменьшать
covariance и делать режимные вероятности чрезмерно уверенными. Величина эффекта
на прогноз сейчас **unknown / requires experiment**.

Новый dataset исправляет также временную опору derived acceleration: разность
двух interval rates делится на расстояние между серединами их интервалов,
(h₁+h₀)/2. Старый v1 делил на текущий h₁. Старые артефакты сохранены; это отдельный
фактор сравнения v1/v2 и причина не переносить scale параметров автоматически.

### Вариант 1: position-only update

Измерительная модель H=[1,0,0]. Использовать только новое settlement observation.
Скорость и ускорение оцениваются state dynamics. Derived diagnostics разрешены
для lagged process-noise adaptation либо для **следующего** prior после текущего
measurement update. Если текущий derived rate сначала влияет на current prior,
а затем тот же z снова входит в likelihood, скрытое повторное использование
информации остаётся. Поэтому адаптация должна быть causally lagged или её
зависимость должна явно входить в probabilistic model.

Плюсы: простая проверяемая информация на update, естественная совместимость с
неравномерным временем, меньше наблюдательных гиперпараметров. Минусы: более
медленное начальное определение velocity, чувствительность к неверному Q и
initial covariance. Эксперимент: нынешние три updates vs position-only при
одинаковых dynamics; затем отдельный position-only + lagged adaptation.
Проверять predictive coverage, innovations, режимную уверенность и не только MAE.

### Вариант 2: joint correlated observation model

Пусть z=(zᵢ,zᵢ₋₁,zᵢ₋₂), h₁=tᵢ-tᵢ₋₁, h₀=tᵢ₋₁-tᵢ₋₂,
m=(h₁+h₀)/2. Для vᵢ=(zᵢ-zᵢ₋₁)/h₁ и aᵢ=(vᵢ-vᵢ₋₁)/m:

```text
[zᵢ, vᵢ, aᵢ]ᵀ = A zᵀ
A = [[1, 0, 0],
     [1/h₁, -1/h₁, 0],
     [1/(h₁m), -(1/h₁+1/h₀)/m, 1/(h₀m)]]
Rjoint = A Σposition Aᵀ
Cov(zᵢ, vᵢ) = σᵢ²/h₁                         (independent raw errors)
Cov(zᵢ, aᵢ) = σᵢ²/(h₁m)
```

Для datum/common-mode errors Σposition не диагональна. Кроме того interval rate
не равен instantaneous velocity at tᵢ, а acceleration имеет свою временную
опору: joint H должен учитывать соответствующие прошлые состояния и динамику.
Воспользоваться единичной H для [position, instantaneous velocity, acceleration]
без поправки на временную опору недостаточно.

**Важное ограничение:** даже верная Rjoint внутри одного окна не устраняет
корреляцию с state prior, уже обновлённым по zᵢ₋₁ и zᵢ₋₂. Нужны augmented/delayed
state, корректная cross-covariance state/noise или batch information update с
однократным учётом исходных позиций. Иначе double counting сохраняется между
соседними окнами. Преобразование A не создаёт новой независимой информации.

Плюсы: можно явно учесть modality/systematic covariance и временную опору.
Минусы: сложность, плохая обусловленность при малых h, требования к covariance,
риск ошибочного повторного assimilation. Начать с oracle linear-Gaussian
simulation test, где position-only и корректный joint вариант эквивалентны по
информации; затем correlated-error и irregular-gap tests. До этого вариант
position-only предпочтительнее как контроль корректности.

## C. Innovation change detection

Detector не является predictor и не получает latent labels при работе.
Использовать стандартизованные **pre-update** innovations eᵢ/√Sᵢ, где Sᵢ учитывает
Δt и прогноз covariance. Для двухстороннего CUSUM возможны
C⁺ᵢ=max(0,C⁺ᵢ₋₁+rᵢ-k), C⁻ᵢ=max(0,C⁻ᵢ₋₁-rᵢ-k).
Назначение такого накопления — обнаружение устойчивого смещения среднего;
[NIST описывает CUSUM и контрольные параметры](https://itl.nist.gov/div898/software/dataplot/refman1/auxillar/cusum.htm).
Числа k/h, число подтверждающих кампаний и robust clipping здесь не назначаются.

Первый эксперимент — diagnostic-only CUSUM. Второй — ограниченное влияние
накопленного сигнала на **следующий** transition prior с фиксированным cap.
GLR с моделью изменения slope может быть третьей, отдельной ablation, если CUSUM
путает постоянный bias и acceleration. Сигнал detector не должен становиться
дополнительным независимым likelihood того же observation.

Порог настраивать по false alarms в обычных train scenarios, включая stable,
изолированные outliers, gaps и measurement shifts. Отчётная единица — alarm на
point-year и detection delay в днях; число наблюдений привести дополнительно.
Robust bounded innovations, persistence требования и spatial concordance —
контролируемые ablations, не гарантированные решения. После gap проверить reset,
elapsed-time discount и непрерывное накопление отдельно. Короткий false spike
не должен быть достаточным доказательством геодинамического режима.

| Случай v2 | Что должно проверяться |
|---|---|
| True smooth/delayed acceleration | Delay, missed events, probability calibration; temporal shape не совпадает с IMM |
| Isolated gross error | Единичный spike не вызывает длительный confident switch |
| Long gap | Рост неопределённости, корректный Δt, отсутствие фиктивного доказательства смены |
| Common systematic shift | Диагностика measurement anomaly; проверка common-mode по профилям |
| Thermal pseudo-transition | Constant latent velocity; false geodynamic alarm и excessive confidence |
| Reactivation / moving focus | Полностью исключены из fit/threshold calibration |

По одному ряду position невозможно в общем случае отличить истинную ступень
поверхности от равной ступени datum: наблюдения математически одинаковы. Detector
может обнаружить изменение, но не доказать его причину. Для уверенной атрибуции
нужны стабильные опоры, независимая modality, сведения об объекте/температуре или
пространственные ограничения. V2 сохраняет эту неоднозначность и не использует
evaluator error components как входы алгоритма.

## Порядок будущих экспериментов

Сначала frozen B1/B5/B6/B7 на v2 без этих изменений. Затем по одному фактору:
Q(Δt), position-only assimilation, diagnostic CUSUM. Joint covariance — после
проверки эквивалентности на аналитическом примере. Комбинацию изменений сравнить
только после isolated ablations. Для B6 также проверить derived-rate assimilation;
иначе изменение наблюдательной модели только у B7 создаст несправедливое сравнение.

Выигрыш нового метода, число ложных переключений и доверительные интервалы
здесь не вычислялись. Обоснование выше — анализ кода и математический design.
