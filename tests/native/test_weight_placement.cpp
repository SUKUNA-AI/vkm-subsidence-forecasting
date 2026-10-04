// Actual original metadata guard; no llama, model values or GPU runtime.
#include "vkm-weight-placement.h"
#include <cassert>
#include <functional>

static vkm::weight_slot slot() {
    vkm::weight_slot v;
    v.name="blk.0.weight"; v.group="blk.0"; v.device_kind="GPU"; v.device_name="CUDA0"; v.buffer_name="CUDA0";
    v.tensor=1; v.storage_tensor=1; v.data=1000; v.storage_data=1000;
    v.buffer=10; v.base=1000; v.buft=11; v.device=12;
    v.tensor_bytes=64; v.storage_bytes=64; v.buffer_bytes=128; v.tensor_type=0;
    v.shape={16,1,1,1}; v.view_chain={1};
    return v;
}
static void rejects(const std::function<void(vkm::weight_slot &)> & mutation) {
    auto v=slot(); mutation(v);
    bool rejected=false;
    try { vkm::require_weight_slot(v); } catch (const std::runtime_error &) { rejected=true; }
    assert(rejected);
}
int main() {
    auto v=slot(); vkm::require_weight_slot(v);
    v.host=true; v.buffer_name="CUDA_Host"; vkm::require_weight_slot(v); // valid HOST, never GPU
    v=slot(); v.device_kind="CPU"; v.device_name="CPU"; v.host=true; vkm::require_weight_slot(v);
    rejects([](auto & x) { x.data=0; });
    rejects([](auto & x) { x.buffer_bytes=0; });
    rejects([](auto & x) { x.storage_bytes=0; });
    rejects([](auto & x) { x.device=0; });
    rejects([](auto & x) { x.device_kind="META"; });
    rejects([](auto & x) { x.buffer_name="CUDA_Split"; });
    rejects([](auto & x) { x.buffer_name="meta"; });
    rejects([](auto & x) { x.base=std::numeric_limits<std::uintptr_t>::max()-32; });
    rejects([](auto & x) { x.data=1100; });
    rejects([](auto & x) { x.view_chain={1,2,1}; x.view_offsets={0,0}; });
    rejects([](auto & x) { x.view_chain={2}; });
    rejects([](auto & x) { x.shape={16,0,1,1}; });
    rejects([](auto & x) { x.view_chain={1,2}; x.view_offsets={32}; x.storage_tensor=2; });
    v=slot(); v.storage_tensor=2; v.view_chain={1,2}; v.view_offsets={16};
    v.storage_bytes=128; v.data=1016; vkm::require_weight_slot(v);
    std::vector<vkm::weight_slot> values={slot()};
    vkm::require_weight_inventory(values,false);
    bool failed=false;
    try { vkm::require_weight_inventory(values,true); } catch (const std::runtime_error &) { failed=true; }
    assert(failed);
    values={slot(),slot()};
    values[0].name=values[1].name="token_embd.weight";
    values[0].group="other"; values[0].host=true;
    values[1].tensor=2; values[1].storage_tensor=2; values[1].view_chain={2};
    values[1].group="output";values[1].buffer=20;values[1].base=values[1].data=values[1].storage_data=2000;
    vkm::require_weight_inventory(values,false); // same name, distinct actual allocations
    v=slot();v.name="token_embd.weight";v.group="output";v.aliases={"INPUT","OUTPUT"};vkm::require_weight_slot(v);
    rejects([](auto & x) { x.group="other";x.aliases={"INPUT","OUTPUT"}; });
    rejects([](auto & x) { x.group="output";x.aliases={"OUTPUT","INPUT"}; });
    values={slot()}; values.push_back(slot()); failed=false;
    try { vkm::require_weight_inventory(values,false); } catch (const std::runtime_error &) { failed=true; }
    assert(failed);
    values.back().name="blk.1.weight"; values.back().device=13; failed=false;
    try { vkm::require_weight_inventory(values,false); } catch (const std::runtime_error &) { failed=true; }
    assert(failed);
}
