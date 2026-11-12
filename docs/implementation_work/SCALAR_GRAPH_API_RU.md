# Фиксированный скалярный граф и редукция Галеркина

Модули `world0.scalar_graph` и `world0.scalar_graph_torch` выделяют уже
использованный column/shear spring оператор. Они не создают backend, каталог
миров или новую схему данных. World ID и binding поступают из существующего
описания мира. Геология, соответствие carrier↔patch/layer, площади, ориентации,
свойства и нагрузки принадлежат вызывающей сборке. В этих модулях нет источников,
путей машины, шумовой геологии или фактических координат рудника.

Класс `ScalarGraph` — самостоятельная scalar MODEL_CHOICE гипотеза, не замена
native tensor FEM. Fine carrier не становится микроядром по имени конечного
элемента. Укрупнённый region×layer — выбранная подсистема с одним q и всей
связанной исходной внутренней памятью. Геологический UNKNOWN не превращается
в известное свойство: optional `unknown_fraction` сохраняется на исходных
носителях; None означает отсутствие предоставленного поля, не нулевую долю.

## Физика и единицы

Для колонки c и слоя l: area [m²], h [m], K/G [Pa], phase fractions [1],
inverse_tau=G/eta [1/s]. q_down [m] — перемещение верхней границы слоя;
нижний соседний DOF имеет индекс+1, под последним слоем q=0. Вертикальные
коэффициенты kb=AK/h, kd=4AG/(3h) [N/m]. Bulk и упругая оставшаяся доля
девиатора входят в K0; каждая активная фаза сохраняет собственные km и z [m].

Латеральный порт задаётся (column_i,column_j,face_area_per_layer,distance_normal).
Его k=G*Aface/dnormal, энергия .5k(qi−qj)². Дальние связи не обрезаются внутри
модуля. Отсутствующая грань означает решение вызывающей физической модели,
а не скрытую оптимизацию. Вертикальная площадь — горизонтальная проекция:
это scalar column law, не нормальная тяга произвольно наклонного 3D-контакта.

`lateral_port` дополнительно принимает unit normal_i_to_j и down_direction,
проверяет базис через существующий `ports.mechanical_port_basis`, вычисляет
работу через `ports.port_power_density`. Две силы на носители противоположны;
их суммарная мощность =−dE_spring/dt. Площадь умножается ровно один раз.
При укрупнении геометрических subfaces сохраняют нормали по отдельности,
а parallel k складывают. Усреднённая фиктивная нормаль не вводится.

Полная энергия и шаг backward Euler:

    Ψ(q,z)=0.5 qᵀK0q +0.5 Σ km (Bq−z)²,
    r=dt*inverse_tau; ke=km/(1+r),
    A=K0+Bᵀdiag(ke)B,
    A q_new=f+Bᵀ(ke*z_old),
    z_new=(z_old+r*Bq_new)/(1+r).

В каждой принятой фазе проверяются сила, recurrence памяти и
F_new·Δq=ΔΨ+Dphysical+Dnumerical. Здесь Dphysical=Σkm*r*(Bq_new−z_new)²;
Dnumerical=.5ΔqᵀK0Δq+.5Σkm*(Δ(Bq−z))². Ft·qt не называется работой шага.
dt=0 — статическое перераспределение при строго неизменной z и времени;
энергия endpoint шага всё ещё отделена от numerical quadrature excess.

Нулевые rates в legacy column builder дают упругую долю, не активную ветвь
памяти. При смене этой гипотезы старую память нельзя молча перепривязать.
UNKNOWN geologic fraction и elastic constitutive remainder — разные понятия.

## API и совместимость

```python
from vkm_world.representations.world0.scalar_graph import ScalarGraph, GraphState

graph = ScalarGraph.from_columns(
    area, heights, bulk, shear, fractions, rates, edges,
    world_id=existing_world_id,
    world_binding_signature=existing_binding_signature,
    unknown_fraction=optional_original_carrier_field,
)
accepted = graph.state(q=q0, memory=z0, time_s=t0)
step = graph.advance(accepted, force_N, dt_s)
accepted = step.state
restored = GraphState.restore(accepted.checkpoint())
```

Это pure transactional API: исключение оставляет old state неизменным; внутри
модели нет скрытого текущего состояния. Checkpoint — обычный runtime payload;
файлы/атомарную запись обслуживает существующий checkpoint storage, новый
storage layer не вводится. Arrays имеют immutable bytes owner; sparse operators
выдаются копиями. Pickle roundtrip восстанавливает объекты через проверяющие
конструкторы и сохраняет неизменяемость buffers. Прямое изменение коэффициента при прежней representation
signature отклоняется. Параметры и UNKNOWN входят в binding фактических данных.

`coarse,P=graph.coarsen(labels)` строит точное Kc=PᵀKfP, Bc=BfP, Fc=PᵀFf.
Для любого qc и ненулевого z сохраняются энергия и виртуальная работа.
Все km/rates/branch_carrier/branch_phase и z сохраняются, даже если одинаковая
поверхность допускает разные внутренние состояния. Это уменьшение числа q,
**не** modal reduction памяти. Physical signature/world ID сохраняются,
representation signature меняется; перенос q/m требует явного отображения.
Точность редукции для произвольного решения не обещается: её оценивают отдельно
от точных Galerkin тождеств на выбранном подпространстве.

Низкоуровневые `build_world/restriction/restrict/integrate` сохранены для
совместимости с выполненными сборками; они возвращают обычные mutable scratch
arrays и не являются accepted-state API. Для production state boundary следует
использовать `ScalarGraph/GraphState`. CPU — прежний sparse CG с column block
preconditioner, применимый к данной SPD spring construction. Это не generic
nonsymmetric solver для HM/LUBBY.

## Torch и повторяемость

```python
from vkm_world.representations.world0.scalar_graph_torch import run_torch
steps, execution = run_torch(graph, accepted, forces, dt_s, device='cpu')
# CUDA: device='cuda', cuda_lease_verifier=caller_verifies_canonical_host_lease
```

Torch импортируется только при вызове, обязательная зависимость для обычного
CPU API отсутствует. Precision строго float64. CUDA требует callback проверки
существующей общей очереди/lease до allocations; библиотека не создаёт второй
lock, не устанавливает runtime и не выдаёт себе разрешение. Callback — часть
политики вызывающего приложения, не криптографическая гарантия владения GPU.

Runner использует dense Cholesky для ограниченных укрупнённых SPD operators,
сохраняя compression одинаковых incidence rows и полную z. `max_dense_bytes`
ограничивает оценку больших dense buffers до их выделения на CPU и устройстве; startup/vendor
workspace и фактический RAM/VRAM контролирует внешний launcher.
Глобальные Torch flags/seed не меняются молча; execution записывает устройство,
версии, precision, deterministic flag, размер памяти и signatures.

CPU oracle включён по умолчанию: одновременно relative≤2e−9 и absolute≤1e−8м
для q и z. Отключение явно возвращает NOT_RUN. Проверки силы/энергии/памяти
не отключаются. Ошибка factorization/лимитов/parity не меняет accepted state.
Restart и перестановку DOF проверяют как отдельные свойства; побитовое совпадение
разных устройств не обещается. Нет выравнивания разных физических топологий
за счёт padding или изменения законов ради batch shape.

Код арифметического GPU шага совпадает по AST с ранее выполненной версией.
Новое device исполнение wrapper следует отличать от прежнего frozen GPU proof.
Синтетические тесты проверяют CPU/Torch-CPU. Отдельный CUDA-smoke wrapper
выполнен на RTX 5070 Ti в float64: относительное отличие q от CPU 2.72e-16,
памяти 1.85e-16; restart совпал точно. Это ограниченный синтетический контроль,
а не проверка всего рудника. Команда, версии и хеши закреплены в отдельном
receipt интегратора. Наличие GPU в системе или импорт Torch не являются PASS.

Модуль импортируется в существующем `vkm_world.representations.world0`, использует
его `ports`, не меняет constitutive/history/native APIs. Старый исторический
текст __init__ о запрете графовой сборки заменён описанием текущего scope;
экспорты существующего API сохранены.
