// Original VKM code. No llama/json dependency: its lock surrounds the actual
// owner's load/destroy, and the getter is evaluated while that lock is held.
#pragma once
#include <cstdint>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>

namespace vkm {
struct loaded_handles {
    std::uintptr_t model = 0, context = 0, mmproj = 0, vocab = 0;
    std::string weights_path, mmproj_path;
    std::uint64_t epoch = 0;
    // Constructed by the live native getter while this lifecycle mutex is held.
    // Empty for older/synthetic callers; no GPU or residency claim is inferred.
    std::string target_weight_placement_json;
};

class loaded_lifetime {
    mutable std::mutex mutex_;
    std::uint64_t epoch_ = 0;
    bool sealed_ = false;
public:
    class mutation {
        loaded_lifetime & owner_;
        std::unique_lock<std::mutex> lock_;
    public:
        explicit mutation(loaded_lifetime & owner) : owner_(owner), lock_(owner.mutex_) {
            owner_.sealed_ = false; // invalidate BEFORE the first actual write
            if (owner_.epoch_ == std::numeric_limits<std::uint64_t>::max()) {
                throw std::runtime_error("loaded lifecycle exhausted");
            }
            ++owner_.epoch_;
        }
        mutation(const mutation &) = delete;
        mutation & operator=(const mutation &) = delete;
        // Call only as the last successful action of a complete initialization.
        void seal() { owner_.sealed_ = true; }
    };

    template<class Getter> loaded_handles snapshot(Getter getter) const {
        // Never park a witness HTTP thread for minutes behind a model load.
        std::unique_lock<std::mutex> lock(mutex_, std::try_to_lock);
        if (!lock.owns_lock() || !sealed_) throw std::runtime_error("loaded lifecycle unavailable");
        auto value = getter(); // live handles and paths, NOT a stored load log
        if (!value.model || !value.context || !value.mmproj || !value.vocab ||
                value.weights_path.empty() || value.mmproj_path.empty()) {
            throw std::runtime_error("loaded lifecycle incomplete");
        }
        value.epoch = epoch_;
        return value;
    }
};

inline bool secret_equal(const std::string & actual, const std::string & expected) {
    if (actual.size() != expected.size()) return false;
    unsigned char different = 0;
    for (std::size_t i = 0; i < expected.size(); ++i) different |= actual[i] ^ expected[i];
    return different == 0;
}
inline bool nonce_valid(const std::string & nonce) {
    if (nonce.size() != 64) return false;
    for (auto c : nonce) if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    return true;
}
} // namespace vkm
