function [device, details] = initialize_device()
assert(license('test','Distrib_Computing_Toolbox') == 1, 'Parallel Computing Toolbox unavailable');
forwardBefore = parallel.gpu.enableCUDAForwardCompatibility;
forwardUsed = false;
initialError = struct();
try
    device = gpuDevice;
catch cause
    initialError = struct('identifier', cause.identifier, 'message', cause.message);
    if contains(cause.identifier, 'UnsupportedDevice', 'IgnoreCase', true) || ...
            strcmp(cause.identifier, 'parallel:gpu:device:DeviceTooNew')
        parallel.gpu.enableCUDAForwardCompatibility(true);
        forwardUsed = true;
        device = gpuDevice;
    else
        rethrow(cause);
    end
end
assert(device.SupportsDouble, 'GPU lacks required FP64 support');
details = struct('name', device.Name, 'compute_capability', device.ComputeCapability, ...
    'total_memory_bytes', device.TotalMemory, 'available_memory_before_bytes', device.AvailableMemory, ...
    'supports_double', device.SupportsDouble, 'forward_compatibility_before', forwardBefore, ...
    'session_forward_compatibility_used', forwardUsed, 'initial_gpu_error', initialError);
end
