"""Domain-vocabulary hit map for a page range of a corpus source.
Usage: python grep_vocab.py <SOURCE_ID> <first> <last>
Prints, per page, the vocabulary groups that hit (to prioritise deep reading; NOT a substitute for reading)."""
import sys, re, pathlib
sid, a, b = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
V = {
 'borehole': r'скважин|скв\.|№\s*\d+[а-я]?\b|разрез|колонк|керн|отбивк|каротаж',
 'strat': r'ПКС|ПДКС|ВЗТ|ВПС|ПЦТ|СМТ|ТКТ|карналлит|сильвинит|галит|пласт\s+[А-ЯA-Z]|Кр\s*I|КрII|Кр\.?\s*II|АБ|пласт[аы]? [ВГДЕ]\b|маркирующ|глинист|ангидрит|мергел|соляно-мергел|терригенно',
 'mech': r'плотност|модул[ья]|Пуассон|прочност|сцеплен|трени|сжати|растяжен|упруг|пластичн|разрушен|повреждён|поврежден',
 'rheo': r'ползуч|реолог|длительн|релаксац|вязк|наследствен|Абел|Нортон|Максвелл|Бюргерс|ядро|конвергенц',
 'stress': r'напряжен|бокового давления|λ\s*=|коэффициент бокового|γH|геостатич|тектоническ',
 'mining': r'камер|целик|панел|блок|выработк|ствол|очистн|проходк|комбайн|шахтное поле|отработ|степень нагружения|коэффициент извлечения|межпласт|защитн',
 'backfill': r'закладк|галитов|отход|гидрозаклад|сухая заклад|заполнен|пустот',
 'subsid': r'оседани|сдвижени|мульд|угол сдвижен|граничн|деформаци[ия] земной|наклон|кривизн|горизонтальн[ыа][ех] деформ',
 'monitor': r'нивелир|репер|профильн|наблюдательн|GNSS|GPS|InSAR|интерферометр|радар|спутник|марк[аи]|станци',
 'geophys': r'георадар|GPR|сейсм|акустическ|Vp|скорост[ьи] (продольн|волн)|диэлектрич|проницаемост|электромагн|гравиметр|электроразвед',
 'hydro': r'водонос|водоупор|рассол|фильтрац|проницаем|гидрогеолог|напор|затоплен|водопроявлен|растворен|выщелач',
 'thermal': r'температур|геотерм|°C|тепл',
 'coords': r'систем[аы] координат|отметк|абсолютн|Балтийск|МСК|СК-42|СК-63|геодезич|датум|высот',
 'chrono': r'\b(19[2-9]\d|20[0-2]\d)\s*(г\.|год|гг)|введен в эксплуатац|начал[оа] отработ|авари',
 'formula': r'=|формул|уравнени',
 'skru': r'СКРУ|СКПРУ|Соликамск|БКПРУ|Березник|Уралкали|Сильвинит|Усольск|Ново-Соликамск',
}
base = pathlib.Path('/home/user/work/corpus')/sid
def page_text(p):
    best = ''
    for cand in [base/f'p{p:04d}.txt', pathlib.Path(f'/home/user/vkm-subsidence-forecasting_resourses/work/ocr/{sid}/p{p:04d}.txt')]:
        if cand.exists():
            t = cand.read_text(encoding='utf-8', errors='replace')
            if len(t.strip()) > len(best.strip()): best = t
    return best
for p in range(a, b+1):
    t = page_text(p)
    hits = {k: len(re.findall(v, t, flags=re.I)) for k, v in V.items()}
    hits = {k: n for k, n in hits.items() if n}
    print(p, len(t), ' '.join(f'{k}:{n}' for k, n in sorted(hits.items(), key=lambda x: -x[1])))
