# Энергия наследственной модели Абеля

Три независимых NumPy-модуля, SI/Mandel xx yy zz xy yz xz. Без геометрии,
данных рудника, абсолютных путей, формул подбора или изменения production law.

- abel_energy.py: positive-spectral witness, analytic tail bounds, immutable
  restart, full-spectrum small-point oracle, signed hybrid correction diagnostic.
- abel_energy_history.py: все фазы и точки заданного virgin-prefix,
  variable dt, dt=0 support loss, weighted Gram full-spectrum Ψ/D/work.
- abel_memory_bounds.py: точный old-prefix drift и bound нового интервала
  через заданный supremum нормы будущего напряжения.

Перенос в существующий vkm_world.representations.world0. Тесты candidate
импортируют эти три модуля локально; три hybrid-теста используют normal PUBLIC
constitutive import. Интегратор меняет только import paths тестов.

Изменения относительно проверенных исходников: имя модулей/imports,
исправление документации small oracle на O(N³) для всех endpoints,
явное отклонение nonfinite материальных коэффициентов в weighted auditor.
Полноспектральная энергия относится к virgin prefix с piecewise-constant stress;
finite SOE energy не равна energy hybrid exact-near law. Его signed defect
не является теплом. Неполная/UNKNOWN предыстория не разрешена как virgin.

Все принятые фазы интегрируются; сохранение pointfields только в выбранных
фазах относится к выводу файлов private driver, а не к математике этих модулей.
Глобальный баланс остальных материалов/флюида/гравитации и получатель энергии
удалённой массы требуют отдельной постановки. Независимая приёмка — у root.
