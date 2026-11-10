function write_json(path, value)
[handle, message] = fopen(path, 'w', 'n', 'UTF-8');
assert(handle >= 0, '%s', message);
cleanup = onCleanup(@() fclose(handle));
fprintf(handle, '%s\n', jsonencode(value, PrettyPrint=true));
end
