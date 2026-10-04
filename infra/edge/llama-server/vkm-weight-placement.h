// Original VKM metadata guard. No GGML/model, inference or GPU dependency.
#pragma once
#include <algorithm>
#include <cstdint>
#include <limits>
#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace vkm {
struct weight_slot {
    std::string name, group, device_kind, device_name, buffer_name;
    std::vector<std::string> aliases;
    std::uintptr_t tensor = 0, storage_tensor = 0, data = 0, storage_data = 0, buffer = 0, base = 0, buft = 0, device = 0;
    std::uint64_t tensor_bytes = 0, storage_bytes = 0, buffer_bytes = 0;
    int tensor_type = -1;
    std::vector<std::int64_t> shape;
    bool host = false;
    std::vector<std::uintptr_t> view_chain;
    std::vector<std::uint64_t> view_offsets;
};

inline void require_weight_slot(const weight_slot & value) {
    const bool layer = value.group.rfind("blk.", 0) == 0 && value.group.size() > 4 && value.group.size() <= 8 &&
        value.group.find_first_not_of("0123456789", 4) == std::string::npos;
    const bool aliased = value.aliases == std::vector<std::string>{"INPUT", "OUTPUT"};
    if (value.name.empty() || value.name.size() > 128 ||
            (value.group != "other" && value.group != "output" && !layer) ||
            (!value.aliases.empty() && (!aliased || value.group != "output")) || value.device_name.empty() ||
            value.device_name.size() > 128 || value.buffer_name.empty() || value.buffer_name.size() > 128 ||
            (value.device_kind != "CPU" && value.device_kind != "GPU") ||
            !value.tensor || !value.storage_tensor || !value.data || !value.storage_data || !value.buffer || !value.base ||
            !value.buft || !value.device || !value.tensor_bytes || !value.storage_bytes || !value.buffer_bytes ||
            value.tensor_type < 0 || value.tensor_type > 1024 || value.shape.size() != 4 ||
            value.view_chain.empty() || value.view_chain.size() > 9 ||
            value.view_chain.front() != value.tensor || value.view_chain.back() != value.storage_tensor ||
            value.view_offsets.size() + 1 != value.view_chain.size() ||
            std::set<std::uintptr_t>(value.view_chain.begin(), value.view_chain.end()).size() != value.view_chain.size()) {
        throw std::runtime_error("target weight metadata unavailable");
    }
    std::string buffer = value.buffer_name;
    std::transform(buffer.begin(), buffer.end(), buffer.begin(), [](unsigned char c) {
        return c >= 'a' && c <= 'z' ? c - ('a' - 'A') : c;
    });
    std::uint64_t offset = 0;
    for (auto part : value.view_offsets) {
        if (part > std::numeric_limits<std::uintptr_t>::max() - offset) throw std::runtime_error("target view offset overflow");
        offset += part;
    }
    for (auto dimension : value.shape) if (dimension <= 0 || dimension > (std::int64_t{1} << 40)) {
        throw std::runtime_error("target tensor shape unavailable");
    }
    if (buffer.find("SPLIT") != std::string::npos || buffer.find("META") != std::string::npos ||
            value.buffer_bytes > std::numeric_limits<std::uintptr_t>::max() - value.base ||
            value.tensor_bytes > value.buffer_bytes || value.data < value.base ||
            value.data - value.base > value.buffer_bytes - value.tensor_bytes ||
            value.storage_bytes > value.buffer_bytes || value.storage_data < value.base ||
            value.storage_data - value.base > value.buffer_bytes - value.storage_bytes ||
            offset > std::numeric_limits<std::uintptr_t>::max() - value.storage_data ||
            value.data != value.storage_data + offset || value.tensor_bytes > value.storage_bytes ||
            offset > value.storage_bytes - value.tensor_bytes) {
        throw std::runtime_error("unsupported target weight allocation");
    }
}

inline void require_weight_inventory(std::vector<weight_slot> & values, bool no_alloc) {
    if (no_alloc || values.empty() || values.size() > 1024) throw std::runtime_error("unallocated target weights");
    std::set<std::pair<std::string, std::uintptr_t>> occurrences;
    std::map<std::uintptr_t, const weight_slot *> allocations;
    for (const auto & value : values) {
        require_weight_slot(value);
        if (!occurrences.emplace(value.name, value.tensor).second) throw std::runtime_error("duplicate target tensor occurrence");
        const auto found = allocations.find(value.buffer);
        if (found != allocations.end()) {
            const auto & before = *found->second;
            if (before.base != value.base || before.buft != value.buft || before.device != value.device ||
                    before.buffer_bytes != value.buffer_bytes || before.host != value.host ||
                    before.device_kind != value.device_kind || before.device_name != value.device_name ||
                    before.buffer_name != value.buffer_name) throw std::runtime_error("target buffer identity conflict");
        } else allocations.emplace(value.buffer, &value);
    }
    std::sort(values.begin(), values.end(), [](const weight_slot & a, const weight_slot & b) {
        return a.name < b.name || (a.name == b.name && a.tensor < b.tensor);
    });
}
} // namespace vkm
