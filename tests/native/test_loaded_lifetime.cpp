// CPU-only test of original lock/lifetime code; no llama, model or GPU library.
#include "vkm-loaded-lifetime.h"
#include <cassert>
#include <thread>

int main() {
    vkm::loaded_lifetime lifetime;
    vkm::loaded_handles actual{1,2,3,4,"weights","mmproj",0,""};
    auto getter = [&] { return actual; };
    auto unavailable = [&] {
        bool failed = false;
        try { lifetime.snapshot(getter); } catch (const std::runtime_error &) { failed = true; }
        assert(failed);
    };
    unavailable();
    {
        vkm::loaded_lifetime::mutation load(lifetime);
        // A concurrent request is bounded/unavailable, not a read of partial handles.
        std::thread observer(unavailable); observer.join();
        load.seal();
    }
    assert(lifetime.snapshot(getter).epoch == 1);
    assert(lifetime.snapshot(getter).mmproj == 3);
    // Snapshot really invokes the current getter, not a saved load receipt.
    actual.mmproj = 9;
    assert(lifetime.snapshot(getter).mmproj == 9);
    {
        vkm::loaded_lifetime::mutation failed_load(lifetime);
        actual.model = 0;
        // Unsuccessful load never seals.
    }
    unavailable();
    {
        vkm::loaded_lifetime::mutation load(lifetime);
        actual.model = 1; load.seal();
    }
    assert(lifetime.snapshot(getter).epoch == 3); // cannot reuse epoch 1
    {
        vkm::loaded_lifetime::mutation destroy(lifetime);
        actual.model = actual.context = actual.mmproj = actual.vocab = 0;
    }
    unavailable();
    assert(vkm::nonce_valid(std::string(64, 'a')));
    assert(!vkm::nonce_valid(std::string(64, 'g')));
    assert(!vkm::nonce_valid(std::string(65, 'a')));
    assert(vkm::secret_equal("secret", "secret"));
    assert(!vkm::secret_equal("secret", "secreT"));
    assert(!vkm::secret_equal("secret", "secret-more"));
}
