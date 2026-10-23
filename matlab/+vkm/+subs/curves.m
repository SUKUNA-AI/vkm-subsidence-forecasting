function c = curves()
%VKM.SUBS.CURVES Таблицы типовых функций сдвижения ВКМ со ссылками на источники.
%   c = vkm.subs.curves() читает общий для Python и MATLAB файл
%   src/vkm_world/physics/data/subsidence_typical_curves.json и возвращает структуру curves:
%   VKM1992_T8_26_MODE1..3, VKM1992_T8_26_ITOGO (VKM-SRC-037 табл. 8.26, печ. с. 339),
%   VKM_UKAZANIYA_T4_SRC065 (VKM-SRC-065 табл. 4, с. 71). Значения — как напечатаны; поле S1_used — расчётное.
persistent cache
if isempty(cache)
    here = fileparts(mfilename('fullpath'));
    f = fullfile(here, '..', '..', '..', 'src', 'vkm_world', 'physics', 'data', 'subsidence_typical_curves.json');
    txt = fileread(f);
    d = jsondecode(txt);
    cache = d.curves;
end
c = cache;
end
