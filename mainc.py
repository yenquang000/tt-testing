import sys
import os
import glob
import subprocess # Used to run external commands (like 'gcc' and the compiled C programs)
import itertools # Used for comparing the two trace logs (zip_longest)
import re  # Used for regular expressions, to parse the trace output from C
import json
import difflib # lexical-based matching library, used to compare variable names and function names for similarity
# import spacy # smeantics-based matching library, used to compare variable names and function names for similarity
# import spacy.cli

# spacy.prefer_gpu()  # Use GPU if available
# nlp = spacy.load("en_core_web_lg")  # Load the large English model for semantic similarity

# Import the necessary components from the clang library
from clang.cindex import Index, Config, TranslationUnit, CursorKind, TypeKind

# This list tells the script where to look for the libclang library file, these are all the common places where tehy can be installed
possible_paths = [
    '/usr/lib/llvm-14/lib/libclang.so',
    '/usr/lib/llvm-13/lib/libclang.so',
    '/usr/lib/llvm-12/lib/libclang.so',
    '/usr/lib/x86_64-linux-gnu/libclang-14.so',
    '/Library/Developer/CommandLineTools/usr/lib/libclang.dylib',
    'C:/Program Files/LLVM/bin/libclang.dll',  
]

SWAP_WINDOW_LINES = [0,1,2,3,4,5]

def setup_libclang():
    #uses the possible paths to check where LLVM is in
    for path in possible_paths:
        if 'LLVM' in path and os.path.exists(path):
            Config.set_library_file(path)  # Tell the clang library where the file is
            print(f"Found libclang at: {path}")
            return True

    # If that fails, just check all paths in the list
    for path in possible_paths:
        if os.path.exists(path):
            Config.set_library_file(path)
            print(f"Found libclang at: {path}")
            return True

    # If no path is found, print an error and return False
    print("Error: libclang not found. Please install LLVM/Clang and add the path")
    print("to 'libclang.dll' (Windows), 'libclang.so' (Linux), or 'libclang.dylib' (macOS)")
    print("to the 'possible_paths' list in this script.")
    return False

def preprocess_c_source(source_text):
    """Normalize C source text before analysis:
    - Strip single-line and multi-line comments
    - Normalize whitespace (tabs to spaces, collapse multiple spaces in code)
    - Strip trailing whitespace per line
    - Remove blank lines at start/end
    - Deduplicate #include directives
    """
    # Strip multi-line comments (/* ... */)
    text = re.sub(r'/\*.*?\*/', '', source_text, flags=re.DOTALL)
    # Strip single-line comments (// ...)
    text = re.sub(r'//[^\n]*', '', text)
    # Replace tabs with 4 spaces
    text = text.replace('\t', '    ')

    seen_includes = set()
    normalized_lines = []
    for line in text.splitlines():
        # Strip trailing whitespace
        line = line.rstrip()
        # Deduplicate #include lines
        stripped = line.strip()
        if stripped.startswith('#include'):
            include_key = re.sub(r'\s+', ' ', stripped)
            if include_key in seen_includes:
                continue
            seen_includes.add(include_key)
        normalized_lines.append(line)

    # Remove leading/trailing blank lines
    while normalized_lines and not normalized_lines[0].strip():
        normalized_lines.pop(0)
    while normalized_lines and not normalized_lines[-1].strip():
        normalized_lines.pop()

    return '\n'.join(normalized_lines) + '\n'


def normalize_source_line(line):
    """Normalize a single C source line for comparison purposes:
    - Collapse whitespace around operators
    - Normalize spacing
    """
    s = line.strip()
    if not s:
        return ''
    # Collapse multiple spaces to single
    s = re.sub(r'\s+', ' ', s)
    # Normalize spaces around common C operators for consistent comparison
    # Assignment and compound assignment
    s = re.sub(r'\s*([+\-*/%&|^]?)=\s*', r' \1= ', s)
    # Fix '= =' back to '=='
    s = re.sub(r'= =', '==', s)
    # Fix '! =' back to '!='
    s = re.sub(r'! =', '!=', s)
    # Fix '> =' and '< =' back to '>=' and '<='
    s = re.sub(r'> =', '>=', s)
    s = re.sub(r'< =', '<=', s)
    # Collapse any double-spaces introduced
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_variable_roles(file_path):
    """Use clang AST to extract variable declarations and map each variable
    to a canonical 'role' based on its position, type, and scope.
    Returns a dict: { (func_name, var_name): canonical_role_label }
    Keys are (function_name, variable_name) tuples to avoid collisions
    when different functions share variable names (e.g. 'i' in two loops).
    """
    index = Index.create()  
    tu = index.parse(file_path, args=['-std=c11'],
                     options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)
    if not tu:
        return {}

    role_map = {}
    func_var_counter = {}  # per-function counters

    for node in tu.cursor.walk_preorder():
        if not node.location.file or node.location.file.name != file_path:
            continue

        if node.kind == CursorKind.FUNCTION_DECL and node.is_definition():
            func_name = node.spelling
            counter = 0
            # Map parameters by position
            for param in node.get_arguments():
                var_type = get_variable_type(param)
                role_label = f"{func_name}_param{counter}_{var_type}"
                role_map[(func_name, param.spelling)] = role_label
                counter += 1
            func_var_counter[func_name] = counter

        elif node.kind == CursorKind.VAR_DECL:
            # Find which function this variable belongs to
            parent = node.semantic_parent
            if parent and parent.kind == CursorKind.FUNCTION_DECL:
                func_name = parent.spelling
            else:
                func_name = "_global"

            var_type = get_variable_type(node)
            count = func_var_counter.get(func_name, 0)
            role_label = f"{func_name}_var{count}_{var_type}"
            role_map[(func_name, node.spelling)] = role_label
            func_var_counter[func_name] = count + 1

    return role_map


def build_role_mapping(ref_roles, bug_roles):
    """Build a mapping from buggy variable names to reference variable names
    based on matching canonical roles.
    Both ref_roles and bug_roles have (func_name, var_name) tuple keys.
    Returns a dict: { (func_name, buggy_var_name): ref_var_name }
    """
    ref_keys = set(ref_roles.keys())
    # Invert ref_roles: role -> (func, ref_var)
    ref_role_to_key = {}
    for (func, var_name), role in ref_roles.items():
        ref_role_to_key[role] = (func, var_name)

    mapping = {}
    for (bug_func, bug_var), bug_role in bug_roles.items():
        if (bug_func, bug_var) in ref_keys:
            mapping[(bug_func, bug_var)] = bug_var
        elif bug_role in ref_role_to_key:
            ref_func, ref_var = ref_role_to_key[bug_role]
            mapping[(bug_func, bug_var)] = ref_var
    return mapping


def normalize_trace_label(label, role_mapping, current_func=None):
    """Normalize a trace label by mapping variable names to their canonical
    reference-file equivalents using the scope-aware role mapping.
    current_func is the name of the function we're currently inside
    (tracked via 'Entering' trace entries)."""
    if not role_mapping:
        return label
    # For labels like 'Entering func_name' or 'Returning', no variable to map
    if label.startswith('Entering') or label.startswith('Returning'):
        return label
    # Look up (current_func, label) in the scoped role mapping
    if current_func and (current_func, label) in role_mapping:
        return role_mapping[(current_func, label)]
    return label
    
def get_text(node):
    start = node.extent.start
    end = node.extent.end

    try:
        # Get the file name from the node's location
        file_name = start.file.name
        # Open the original source file
        with open(file_name, 'r') as f:
            f.seek(start.offset)  # Go to the start byte of the node
            # Read the exact number of bytes that make up the node
            return f.read(end.offset - start.offset)
    except Exception as e:
        # Failsafe in case the file isn't found or text can't be read
        return ""  # Return empty string on failure


def get_variable_type(node):
    """Utility to get the simplified type of a C variable (e.g., 'int', 'float')."""
    type_name = node.type.spelling  # The type as a string, e.g., "int", "int [5]"
    type_kind = node.type.kind  # The type as an enum, e.g., TypeKind.INT, TypeKind.POINTER

    # We use the robust TypeKind enum to check if it's any kind of pointer or array.
    # We want to avoid tracing these, as they just print memory addresses.
    if type_kind == TypeKind.POINTER or type_kind == TypeKind.CONSTANTARRAY or type_kind == TypeKind.INCOMPLETEARRAY or type_kind == TypeKind.VARIABLEARRAY:
        return 'pointer'

    # Fallback to checking the string name as well to check if its a pointer (just in case case above doesn't work)
    if '[]' in type_name or '*' in type_name:
        return 'pointer'
    # If it's not a pointer, check for basic types
    if 'int' in type_name:
        return 'int'
    if 'float' in type_name:
        return 'float'
    if 'double' in type_name:
        return 'double'
    if 'string' in type_name:
        return 'string'
    return 'other'  # Default for structs, etc. we don't want to trace


def get_printf_format(var_type): #This function just gets the right printf format
    if var_type == 'int':
        return '%d' 
    if var_type == 'float':
        return '%f' 
    if var_type == 'double':
        return '%lf'
    if var_type == 'string': # if this section failed then user failed to provide the string library, this would be a case of incorrect recall
        return '%s'
    return '%p'


def instrument_c_code(input_file, output_file): # This function goes through the C file and puts print statements before writing new c file
    # Create an index, which is the entry point to the clang library
    index = Index.create()
    # Parse the C file into a "Translation Unit" (the complete AST for that file)
    tu = index.parse(input_file, args=['-std=c11'],  # Use standard C11
                     options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD)

    if not tu:  # Handle parsing failure
        print(f"Error: Unable to parse {input_file}")
        return False

    # Reads original source code
    with open(input_file, 'r') as f:
        source_lines = list(f)

    # This dictionary will store our injections.
    injections = {}
    loop_body_injected_lines = set()

    def add_injection(line, text): #adds the injections into the code
        if line not in injections:
            injections[line] = []
        injections[line].append(text)

    # Traverses through the Tree via Pre-Order
    for node in tu.cursor.walk_preorder():
        # Ensure the node is from our main file not an included header (like <stdio.h>)
        if not node.location.file or node.location.file.name != input_file:
            continue

        line = node.location.line  # Get the line number for the current node
        if not line:  # Skip if no valid location
            continue

        # Check if the node is a Function Declaration and is also a "definition" (has a body)
        if node.kind == CursorKind.FUNCTION_DECL and node.is_definition():
            func_name = node.spelling  # Get the function's name (e.g., "calculate_average")
            body_start_line = -1
            try:
                # Find the body (a "Compound Statement" node, which is the { ... } block)
                compound_stmt = next(
                    c for c in node.get_children() if c.kind == CursorKind.COMPOUND_STMT)
                body_start_line = compound_stmt.extent.start.line  # Get line number of '{'
            except StopIteration:
                continue  # Skip functions without a body

            # Create the trace string for function entry
            # We add fflush(stdout) to force C to print immediately.
            # Captures log before crash due to assert
            # Always inject the Entering trace first
            add_injection(body_start_line, (
                f'    printf("TRACE:L{body_start_line}:Entering {func_name}\\n"); '
                f'fflush(stdout);\n'
            ))

            # Then inject each non-pointer parameter separately
            for param in node.get_arguments():
                var_name = param.spelling
                var_type = get_variable_type(param)
                if var_type == 'pointer':
                    continue
                printf_format = get_printf_format(var_type)
                add_injection(body_start_line, f'    printf("TRACE:L{body_start_line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n')

        elif node.kind == CursorKind.VAR_DECL:
            var_name = node.spelling
            var_type = get_variable_type(node)
            if var_type == 'pointer': #don't trace pointers
                continue
            is_loop_var = False
            for ancestor in tu.cursor.walk_preorder():
                if ancestor.kind == CursorKind.FOR_STMT:
                    # The init clause of a for loop comes before the body
                    # Check if this VAR_DECL's location is inside the for stmt
                    # but NOT inside its body (COMPOUND_STMT)
                    for_start = ancestor.extent.start.offset
                    for_end = ancestor.extent.end.offset
                    node_offset = node.extent.start.offset
                    if for_start <= node_offset <= for_end:
                        try:
                            body = next(c for c in ancestor.get_children()
                                       if c.kind == CursorKind.COMPOUND_STMT)
                            body_start = body.extent.start.offset
                            if node_offset < body_start:
                                is_loop_var = True
                                break
                        except StopIteration:
                            pass
            if is_loop_var:
                continue
            # Check if it has an initializer (e.g., '= 0')
            if any(c.kind.is_expression() for c in node.get_children()):
                printf_format = get_printf_format(var_type)
                inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
                add_injection(line, inject_text)  # Inject *after* this line
        elif node.kind.is_expression() and node.kind.name == 'BINARY_OPERATOR':
            op_text = get_text(node)  # Get the text, e.g., "avg = (float)total / count"
            # Check for assignment operators (but not '==')
            if '=' in op_text and '==' not in op_text:
                # Get the left-hand side (the variable being assigned to)
                lhs = list(node.get_children())[0]
                var_name = get_text(lhs)
                if not var_name:
                    continue 

                var_type = get_variable_type(lhs)
                if var_type == 'pointer':
                    continue
                
                printf_format = get_printf_format(var_type)
                inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
                add_injection(line, inject_text)  
        
        elif node.kind == CursorKind.COMPOUND_ASSIGNMENT_OPERATOR: 
            if line in loop_body_injected_lines:
                continue
            lhs = list(node.get_children())[0] 
            var_name = get_text(lhs)
            if not var_name:
                continue
            
            var_type = get_variable_type(lhs)
            if var_type == 'pointer':
                continue
                
            printf_format = get_printf_format(var_type)
            inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
            add_injection(line, inject_text) 

        elif node.kind.is_expression() and node.kind.name == 'UNARY_OPERATOR':
            if line in loop_body_injected_lines:
                continue
            op_text = get_text(node)
            if '++' in op_text or '--' in op_text: 
                child = list(node.get_children())[0] 
                var_name = get_text(child)
                if not var_name:
                    continue

                var_type = get_variable_type(child)
                if var_type == 'pointer':
                    continue
                    
                printf_format = get_printf_format(var_type)
                inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
                add_injection(line, inject_text) 

        elif node.kind == CursorKind.RETURN_STMT:
            children = list(node.get_children())
            if children: 
                return_val_node = children[0]
                return_val_text = get_text(return_val_node) 
                if not return_val_text:
                    continue

                var_type = get_variable_type(return_val_node)
                if var_type == 'pointer':
                    continue
                    
                printf_format = get_printf_format(var_type)
                inject_text = f'    printf("TRACE:L{line}:Returning={printf_format}\\n", {return_val_text}); fflush(stdout);\n'
                add_injection(line, inject_text)
            else:
                inject_text = f'    printf("TRACE:L{line}:Returning=(void)\\n"); fflush(stdout);\n'
                add_injection(line, inject_text) 
                
    source_text = ''.join(source_lines)
    has_stdio = '#include <stdio.h>' in source_text or '#include<stdio.h>' in source_text
    has_assert = '#include <assert.h>' in source_text or '#include<assert.h>' in source_text

    with open(output_file, 'w') as f:
        if not has_stdio:
            f.write('#include <stdio.h>\n')
        if not has_assert:
            f.write('#include <assert.h>\n')
        f.write('\n')

        for i, line_text in enumerate(source_lines):
            current_line_num = i + 1  # Line numbers are 1-based

            # Check for injections that go *before* this line (e.g., return)
                        # Check for injections that go *before* this line (e.g., return)
            if current_line_num in injections and any("Returning" in inj for inj in injections[current_line_num]):
                stripped = line_text.strip()
                if not (stripped.startswith('for ') or stripped.startswith('for(')):
                    for injection in injections[current_line_num]:
                        if "Returning" in injection:
                            indentation = len(line_text) - len(line_text.lstrip(' '))
                            f.write(' ' * indentation + injection)

            # Write the original line itself
            f.write(line_text)

            # Check for injections that go *after* this line (e.g., assign, func entry)
            if current_line_num in injections and not any("Returning" in inj for inj in injections[current_line_num]):
                stripped = line_text.strip()
                if not (stripped.startswith('for ') or stripped.startswith('for(')):
                    for injection in injections[current_line_num]:
                        indentation = len(line_text) - len(line_text.lstrip(' '))
                        if '{' in line_text and any("Entering" in inj for inj in injections[current_line_num]):
                            indentation += 4
                        f.write(' ' * indentation + injection)

    return True  # Success


def compile_c_code(c_file, exe_file):
    compiler = 'gcc'  # Try gcc first
    try:
        # Run 'gcc -v' to see if it exists. capture_output=True hides the output.
        subprocess.run([compiler, '-v'],
                       capture_output=True, check=True, text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        compiler = 'clang'  # Try clang if gcc fails
        try:
            subprocess.run([compiler, '-v'],
                           capture_output=True, check=True, text=True)
        except (subprocess.CalledProcessError, FileNotFoundError):
            print("Error: No C compiler (gcc or clang) found in PATH.")
            return False

    print(f"Compiling {c_file} using {compiler}...")
    try:
        # Run the compile command, e.g., "gcc test.traced.c -o test_app"
        subprocess.run([compiler, c_file, '-o', exe_file],
                       check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        # If compilation fails (e.g., syntax error), print the C compiler's error
        print(f"Compilation failed for {c_file}:")
        print(e.stderr)
        print("Error: Incorrect Recall, incorrect syntax")
        return False
    return True  # Success

def _is_swap_valid(lines, tmp_path="swap_test_check.c"):
   try:
       with open(tmp_path, "w") as f:
           f.write('#include <stdio.h>\n')
           f.write('#include <assert.h>\n')
           f.writelines(lines)
       result = compile_c_code(tmp_path, "swap_test_exe")
       return result
   except Exception:
       return False
   finally:
       import time
       for path in [tmp_path, "swap_test_exe", "swap_test_exe.exe"]:
           for _ in range(5):
                try:
                    # clean up temp files
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                    if os.path.exists("swap_test_exe"):
                        os.remove("swap_test_exe")
                    if os.path.exists("swap_test_exe.exe"):
                        os.remove("swap_test_exe.exe") 
                except PermissionError:
                   time.sleep(0.1)  


def run_c_executable(exe_file):
    """Runs a compiled C executable and returns its captured stdout."""
    # Command is './exe_file' on Linux/macOS and '.\exe_file' on Windows
    run_command = f"./{exe_file}" if os.name != 'nt' else f".\\{exe_file}"

    try:
        # Run the command. check=True makes it raise an error if the program crashes.
        result = subprocess.run(
            run_command, check=True, capture_output=True, text=True, shell=True)
        return result.stdout  # Return the text output from the C program
    except subprocess.CalledProcessError as e:
        # This block catches crashes (e.g., our "Assertion failed")
        print(f"Execution failed for {exe_file}:")
        print(e.stderr)  # Print the error message (e.g., "Assertion failed: ...")
        # Return the stdout captured *before* the crash. This is why we used fflush!
        return e.stdout
    finally:
        # This block runs whether the program succeeded or crashed
        # Clean up executable file
        exe_path = exe_file
        if os.name == 'nt' and not exe_file.endswith('.exe'):
            exe_path = f"{exe_file}.exe"  # Add .exe on Windows

        if os.path.exists(exe_path):
            os.remove(exe_path)  # Delete the compiled .exe file


def parse_trace_log(stdout):
    """Parses 'TRACE:var=val' lines from the C program's stdout."""
    log = []  # Start with an empty log
    # Define regular expressions to find lines starting with "TRACE:"
    pattern_with_val = re.compile(r'^TRACE:L(\d+):(.*?)=(.*)$')
    pattern_noval = re.compile(r'^TRACE:L(\d+):(.*)$')

    for raw_line in stdout.splitlines():  # Loop over each line of output
        line = raw_line.strip()
        if not line.startswith("TRACE:"):
            continue # Skip lines that don't start with "TRACE:"

        match = pattern_with_val.match(line) # See if the line matches our regex
        if match:
            # If it matches, "groups()" gives us the two captured parts
            lineno = int(match.group(1))
            label = match.group(2).strip()
            val = match.group(3).strip()
            log.append((lineno, label, val)) # Add (variable, value) tuple to log
            continue
        else:
            match2 = pattern_noval.match(line)
            if match2:
                lineno = int(match2.group(1))
                label = match2.group(2).strip()
                log.append((lineno, label, None))
    return log


def normalize_trace_value(val):
    """Normalize a trace value string for robust comparison.
    Handles int/float representation differences like '100' vs '100.000000'."""
    if val is None:
        return None
    val = val.strip()
    try:
        num = float(val)
        # If it's effectively an integer, normalize to int string
        if num == int(num):
            return str(int(num))
        # Otherwise round to 6 decimal places to avoid floating-point noise
        return f"{num:.6f}".rstrip('0').rstrip('.')
    except (ValueError, OverflowError):
        return val


def compare_trace_values(ref_val, bug_val):
    """Compare two trace values with numeric awareness.
    Returns True if they match, False if they differ."""
    if ref_val is None and bug_val is None:
        return True
    if ref_val is None or bug_val is None:
        return False
    # Fast path: exact string match
    if ref_val == bug_val:
        return True
    # Normalize and compare
    return normalize_trace_value(ref_val) == normalize_trace_value(bug_val)


def compare_source_lines(ref_path, buggy_path):
    """Fallback: compare source files line-by-line when trace-based comparison
    cannot run (e.g., compilation or execution failure).
    Uses normalized comparison to ignore formatting differences."""
    print("\n SOURCE-LEVEL COMPARISON (fallback)")
    print("Trace-based comparison unavailable. Comparing source lines directly...\n")

    with open(ref_path, 'r') as f:
        ref_lines = f.readlines()
    with open(buggy_path, 'r') as f:
        bug_lines = f.readlines()

    diffs = []
    max_len = max(len(ref_lines), len(bug_lines))
    for i in range(max_len):
        ref_line = ref_lines[i].rstrip() if i < len(ref_lines) else "(missing)"
        bug_line = bug_lines[i].rstrip() if i < len(bug_lines) else "(missing)"

        # Normalize both lines for comparison
        ref_normalized = normalize_source_line(ref_line)
        bug_normalized = normalize_source_line(bug_line)

        # Skip empty lines (after normalization)
        if not ref_normalized and not bug_normalized:
            continue

        if ref_normalized != bug_normalized:
            diff_info = {
                "source_line": i + 1,
                "ref_text": ref_line,
                "bug_text": bug_line,
            }
            diffs.append(diff_info)
            print(f"  Line {i+1}:")
            print(f"    Reference: {ref_line}")
            print(f"    Buggy:     {bug_line}")

    if not diffs:
        print("No source-level differences found.")
        return False, []

    print(f"\nFound {len(diffs)} source-level difference(s).")
    return True, diffs

def score_entry_match(ref_entry, bug_entry, role_mapping, bug_func):
    """Score how well two trace entries match each other.
    Returns a number: higher = better match, negative = mismatch."""
    if ref_entry is None or bug_entry is None:
        return -1  

    r_line, r_label, r_val = ref_entry
    b_line, b_label, b_val = bug_entry

   
    mapped = role_mapping.get((bug_func, b_label), b_label)
    labels_match = (r_label == mapped) or (r_label == b_label)

    if not labels_match:
        return -2 


    if r_label.startswith('Entering') or r_label.startswith('Returning'):
        if r_val is None and b_val is None:
            return 5 
        if r_val is not None and b_val is not None:
            return 4 if normalize_trace_value(r_val) == normalize_trace_value(b_val) else 3

    if r_val is None and b_val is None:
        return 2
    if r_val is not None and b_val is not None:
        if normalize_trace_value(r_val) == normalize_trace_value(b_val):
            return 3 
        else:
            return 2  
    return 1 


def align_trace_logs(ref_log, buggy_log, role_mapping=None):
    """Align two trace logs using dynamic programming (similar to sequence alignment).
    This handles cases where ref and buggy have different structure — extra calls,
    renamed variables, reordered statements — without falling apart after one mismatch.
    Returns pairs of (ref_entry_or_None, bug_entry_or_None)."""
    if role_mapping is None:
        role_mapping = {}

    n = len(ref_log)
    m = len(buggy_log)

    
    bug_func_at = {}  
    bug_func = None
    for bi, (b_line, b_label, b_val) in enumerate(buggy_log):
        if b_label.startswith('Entering '):
            bug_func = b_label.split(' ', 1)[1]
        bug_func_at[bi] = bug_func

    GAP = -1  

    
    # dp[i][j] = aligning ref_log[:i] with buggy_log[:j]
    dp = [[0] * (m + 1) for _ in range(n + 1)]

   
    for i in range(1, n + 1):
        dp[i][0] = dp[i-1][0] + GAP
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j-1] + GAP

    
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            bf = bug_func_at.get(j - 1)
            match_score = score_entry_match(ref_log[i-1], buggy_log[j-1], role_mapping, bf)
            dp[i][j] = max(
                dp[i-1][j-1] + match_score, 
                dp[i-1][j] + GAP,             
                dp[i][j-1] + GAP              
            )

  
    aligned = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0:
            bf = bug_func_at.get(j - 1)
            match_score = score_entry_match(ref_log[i-1], buggy_log[j-1], role_mapping, bf)
            if dp[i][j] == dp[i-1][j-1] + match_score:
                aligned.append((ref_log[i-1], buggy_log[j-1]))
                i -= 1
                j -= 1
                continue
        if i > 0 and dp[i][j] == dp[i-1][j] + GAP:
            aligned.append((ref_log[i-1], None))
            i -= 1
        else:
            aligned.append((None, buggy_log[j-1]))
            j -= 1

    aligned.reverse() 
    return aligned

def compare_trace_logs(ref_log, buggy_log, **kwargs):
    print("\n TRACE COMPARISON")
    print("Comparing logs to find the first point of divergence...\n")

    if not ref_log or not buggy_log:
        print("Error: Could not generate one or both trace logs. Exiting.")
        return False, None, None, None, None, []

    role_mapping = kwargs.get('role_mapping', {})

    if len(buggy_log) < len(ref_log) * 0.5:
        print("Warning: Buggy log is significantly shorter than reference — likely an early crash.")
        last_matched = buggy_log[-1] if buggy_log else None
        if last_matched:
            b_line, b_label, b_val = last_matched
            crash_diff = {
                "trace_index": len(buggy_log),
                "function": b_label.split(' ', 1)[1] if b_label.startswith('Entering') else None,
                "ref_line": None,
                "bug_line": b_line,
                "ref_var": "(Missing)",
                "bug_var": b_label,
                "ref_val": None,
                "bug_val": b_val,
                "note": "Program likely crashed here"
            }
            print(f"Last entry before crash: '{b_label}' at line {b_line}")
            try:
                with open("trace_differences.json", "w") as f:
                    json.dump([crash_diff], f, indent=2)
            except Exception as e:
                print(f"Could not save to JSON file: {e}")
            return True, b_line, b_label, None, b_val, [crash_diff]

    aligned_pairs = align_trace_logs(ref_log, buggy_log, role_mapping=role_mapping)
    diffs = []
    call_stack = []
    current_func = None
    returning_func = None

    for i, (ref_entry, buggy_entry) in enumerate(aligned_pairs):
        ref_line = ref_entry[0] if ref_entry else None
        ref_var  = ref_entry[1] if ref_entry else "(Missing)"
        ref_val  = ref_entry[2] if ref_entry else None
        bug_line = buggy_entry[0] if buggy_entry else None
        bug_var  = buggy_entry[1] if buggy_entry else "(Missing)"
        bug_val  = buggy_entry[2] if buggy_entry else None

        if buggy_entry and bug_var.startswith('Entering '):
            current_func = bug_var.split(' ', 1)[1]
            call_stack.append(current_func)
        elif ref_entry and ref_var.startswith('Entering '):
            current_func = ref_var.split(' ', 1)[1]
            call_stack.append(current_func)
        elif buggy_entry and bug_var.startswith('Returning'):
            returning_func = call_stack[-1] if call_stack else None
            if call_stack:
                call_stack.pop()
            current_func = call_stack[-1] if call_stack else None

        is_returning = bug_var.startswith('Returning') if bug_var else False
        func_for_diff = returning_func if is_returning else current_func

    
        if ref_entry is None or buggy_entry is None:
            diffs.append({
                "trace_index": i,
                "function": func_for_diff,
                "ref_line": ref_line, "bug_line": bug_line,
                "ref_var": ref_var,   "bug_var": bug_var,
                "ref_val": ref_val,   "bug_val": bug_val
            })
            continue

        ref_var_normalized = normalize_trace_label(ref_var, {})
        bug_var_normalized = normalize_trace_label(bug_var, role_mapping, current_func=current_func)

        if ref_var_normalized != bug_var_normalized:
            diffs.append({
                "trace_index": i,
                "function": func_for_diff,
                "ref_line": ref_line, "bug_line": bug_line,
                "ref_var": ref_var,   "bug_var": bug_var,
                "ref_val": ref_val,   "bug_val": bug_val
            })
            continue

       
        if not compare_trace_values(ref_val, bug_val):
            diffs.append({
                "trace_index": i,
                "function": func_for_diff,
                "ref_line": ref_line, "bug_line": bug_line,
                "ref_var": ref_var,   "bug_var": bug_var,
                "ref_val": ref_val,   "bug_val": bug_val
            })


    ref_var_values = {}
    for _, label, val in ref_log:
        if val is not None and not label.startswith('Entering') and not label.startswith('Returning'):
            ref_var_values.setdefault(label, set()).add(normalize_trace_value(val))

    bug_var_values = {}
    for _, label, val in buggy_log:
        if val is not None and not label.startswith('Entering') and not label.startswith('Returning'):
            bug_var_values.setdefault(label, set()).add(normalize_trace_value(val))

    filtered_diffs = []
    for d in diffs:
        ref_var = d["ref_var"]
        bug_var = d["bug_var"]
        ref_val_n = normalize_trace_value(d["ref_val"]) if d["ref_val"] else None
        bug_val_n = normalize_trace_value(d["bug_val"]) if d["bug_val"] else None

        if ref_var == "(Missing)":
            if bug_var in ref_var_values and bug_val_n in ref_var_values.get(bug_var, set()):
                continue
        elif bug_var == "(Missing)":
            if ref_var in bug_var_values and ref_val_n in bug_var_values.get(ref_var, set()):
                continue

        filtered_diffs.append(d)
    diffs = filtered_diffs

    seen_lines = set()
    unique_diffs = []
    for d in diffs:
        line_key = (d["ref_line"], d["bug_line"], d["ref_var"], d["bug_var"])
        if line_key in seen_lines:
            continue
        seen_lines.add(line_key)
        unique_diffs.append(d)
    diffs = unique_diffs

    
    seen_func_val_pairs = {}
    final_diffs = []
    for d in diffs:
        func_key = d.get("function")
        val_pair = (
            normalize_trace_value(d["ref_val"]),
            normalize_trace_value(d["bug_val"])
        )
        if func_key not in seen_func_val_pairs:
            seen_func_val_pairs[func_key] = set()
        if val_pair in seen_func_val_pairs[func_key]:
            continue  
        seen_func_val_pairs[func_key].add(val_pair)
        final_diffs.append(d)
    diffs = final_diffs

    if not diffs:
        print("No differences found in trace logs. The logic appears identical.")
        return False, None, None, None, None, []

    print(f"Found {len(diffs)} unique trace difference(s):\n")
    for di, d in enumerate(diffs):
        func_label = d.get("function") or "unknown"
        line_label = d["bug_line"] if d["bug_line"] is not None else d["ref_line"]
        print(f"  [{di+1}] Function: {func_label}, Line: {line_label}, "
              f"Var: {d['bug_var']}, Ref={d['ref_val']}, Bug={d['bug_val']}")

    try:
        with open("trace_differences.json", "w") as f:
            json.dump(diffs, f, indent=2)
    except Exception as e:
        print(f"Could not save to JSON file: {e}")

    first_diff = diffs[0]
    first_line = first_diff["bug_line"] if first_diff["bug_line"] is not None else first_diff["ref_line"]
    first_var  = first_diff["bug_var"] if first_diff["bug_var"] != "(Missing)" else first_diff["ref_var"]
    first_ref_val = first_diff["ref_val"]
    first_bug_val = first_diff["bug_val"]
    return True, first_line, first_var, first_ref_val, first_bug_val, diffs
def swap_code_region_between_files(
      reference_path, buggy_path, center_line,
      window=0,
      reference_out_path = "reference_swapped.c", buggy_out_path="sample_swapped.c"
):
  with open(reference_path, "r") as f:
      ref_lines = f.readlines()
  with open(buggy_path, "r") as f:
      bug_lines = f.readlines()
  if center_line is None:
      return reference_out_path, buggy_out_path
  # convert the 1-based C line number to a 0-based Python list index
  target_idx = center_line - 1
  print(f"\nFinding diffs around line {center_line}...")
  # initialize the SequenceMatcher
  #autojunk=False prevents difflib from ignoring blank lines or brackets
  matcher = difflib.SequenceMatcher(None, ref_lines, bug_lines, autojunk=False)




  # get_opcodes() returns instructions on how to turn the reference into the buggy file
 #i1 and i2 are start and end of the reference file
 #j1 and j2 are start and end of the buggy file
  candidates = [] #of broken code block
 
  for tag, i1, i2, j1, j2 in matcher.get_opcodes():
      if tag not in ('replace', 'delete', 'insert'):
          continue 
      if j1 <= target_idx <= j2:
          distance = 0  # target is inside this block
      else:
          distance = min(abs(target_idx - j1), abs(target_idx - j2))
    
      # only consider blocks within the window
      if distance <= window:
          candidates.append((distance, tag, i1, i2, j1, j2))
  if not candidates:
      print(f"No diff block found within ±{window} lines of line {center_line}.")
      return None, None
 #select the candidate for patching
  tag_priority = {'replace': 0, 'delete': 1, 'insert': 2}
 #sort the list by distance(in ascending order), then by tag priority
  candidates.sort(key=lambda c: (c[0], tag_priority.get(c[1], 3)))
  distance, tag, i1, i2, j1, j2 = candidates[0]
  print(f"-> Best match Diff type: '{tag}', distance: {distance} lines")
  swap_size = j2 - j1 
  swapped_lines = None


  while swap_size >= 1:
      
       attempt_j1 = max(j1, target_idx - swap_size // 2)
       attempt_j2 = min(j2, attempt_j1 + swap_size)
       attempt_i1 = i1
       attempt_i2 = min(i2, i1 + swap_size)


       candidate_lines = bug_lines[:attempt_j1] + ref_lines[attempt_i1:attempt_i2] + bug_lines[attempt_j2:]


       print(f"-> Trying swap of {swap_size} line(s) (buggy {attempt_j1+1}-{attempt_j2}, ref {attempt_i1+1}-{attempt_i2})...")


       if _is_swap_valid(candidate_lines):
           print(f"-> Swap of {swap_size} line(s) compiled successfully!")
           swapped_lines = candidate_lines
           break
       swap_size -= 1  # get smaller after every iteration
  if swapped_lines is None:
       print(f"-> No valid swap found")
       swapped_lines = bug_lines
  with open(reference_out_path, "w") as f:
      f.writelines(ref_lines)
  with open(buggy_out_path, "w") as f:
      f.writelines(swapped_lines)


  return reference_out_path, buggy_out_path






# def insert_assert_at_line(src_path, dst_path, line_no, var_name, ref_val_str):
#     with open(src_path, "r") as f:
#         lines = f.readlines()
#     if line_no <1 or line_no > len(lines):
#         print(f"Warning: line {line_no} is out of range for file {src_path}. No assert inserted.")
#         with open(dst_path, "w") as f:
#             f.writelines(lines)
#         return dst_path
#     idx = line_no - 1
#     original_line = lines[idx]
#     indentation = len(original_line)-len(original_line.lstrip(' '))

#     is_float = any(ch in ref_val_str for ch in ['.', 'e', 'E'])
#     if is_float:
#         assert_code = f'{" " * indentation}assert({var_name} == {ref_val_str});\n'
#     else:
#         assert_code = f'{" " * indentation}assert({var_name} == {ref_val_str});\n'  # add logic later for other ones that aren't float, maybe this section isn't needed

#     lines.insert(idx+1, assert_code)
#     with open(dst_path, "w") as f:
#         f.writelines(lines)
#     print(f"Inserted assert at line {line_no} in {dst_path}: {assert_code.strip()}")
#     return dst_path
def clean():
    temp_extensions = [
        "*.traced.c",
        "reference_to_trace.c",
        "sample_to_trace.c",
        "reference_swapped*.c",
        "ref_swapped*.c",
        "test_swapped*.c",
        "sample_swapped*.c",
        "*_app",
        "*.exe",
    ]
    for pattern in temp_extensions:
        for path in glob.glob(pattern):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Failed deleting path {path}: {e}")
def main():
    # This block only runs when you execute "python c_differential_tracer.py"
    if not setup_libclang():
        sys.exit(1)  # Exit if libclang is not found

    with open("ref.c", "r") as f:
        referenceCode = f.read()
    with open("stu.c", "r") as f:
        buggyCode = f.read()
    ref_file = "reference_to_trace.c"
    test_file = "sample_to_trace.c"

    # Write the strings above into actual .c files
    with open(ref_file, "w") as f:
        f.write(referenceCode)
    with open(test_file, "w") as f:
        f.write(buggyCode)

     # Extract variable roles from both files for cross-file name normalization
    print("\nExtracting variable roles for normalization...")
    ref_roles = extract_variable_roles(ref_file)
    bug_roles = extract_variable_roles(test_file)
    role_mapping = build_role_mapping(ref_roles, bug_roles)
    if role_mapping:
        renamed = {f"{func}:{var}": ref_var for (func, var), ref_var in role_mapping.items() if var != ref_var}
        if renamed:
            print(f"Variable name mapping (buggy -> ref): {renamed}")
        else:
            print("Variable names already match between files.")
    else:
        print("No variable role mapping could be built.")

    # Process Reference File
    print(f" Processing Reference File: {ref_file} ")
    traced_ref_file = "ref.traced.c"  # The new file we will create
    ref_exe = "ref_app"  # The compiled executable we will create
    ref_log = None
    ref_pipeline_ok = False
    if instrument_c_code(ref_file, traced_ref_file):  # Create ref.traced.c
        if compile_c_code(traced_ref_file, ref_exe):  # Compile ref.traced.c
            stdout = run_c_executable(ref_exe)  # Runs code
            if stdout is not None:
                print("\nCaptured Reference Output:\n" + stdout)
                ref_log = parse_trace_log(stdout)  # Parse the log
                ref_pipeline_ok = True

    if not ref_pipeline_ok:
        print("Warning: Reference file pipeline failed (instrument/compile/run).")

    # Process Buggy File
    print(f"\nProcessing Buggy File: {test_file}")
    traced_test_file = "test.traced.c"
    test_exe = "test_app"
    buggy_log = None
    buggy_pipeline_ok = False
    if instrument_c_code(test_file, traced_test_file):  
        if compile_c_code(traced_test_file, test_exe): 
            stdout = run_c_executable(test_exe)  # (This will crash due to assert)
            if stdout is not None:
                # This will print the partial log captured before the crash
                print("\nCaptured Buggy Output (up to crash):\n" + stdout)
                buggy_log = parse_trace_log(stdout)
                buggy_pipeline_ok = True

    if not buggy_pipeline_ok:
        print("Warning: Buggy file pipeline failed (instrument/compile/run).")

    # If either pipeline failed completely, fall back to source-level comparison
    if not ref_pipeline_ok or not buggy_pipeline_ok:
        print("\nFalling back to source-level comparison...")
        found_source_diff, source_diffs = compare_source_lines(ref_file, test_file)
        if found_source_diff:
            # Build diffs in the same format so the swap logic can use them
            diffs = []
            for sd in source_diffs:
                diffs.append({
                    "trace_index": None,
                    "function": None,
                    "ref_line": sd["source_line"],
                    "bug_line": sd["source_line"],
                    "ref_var": sd["ref_text"].strip(),
                    "bug_var": sd["bug_text"].strip(),
                    "ref_val": sd["ref_text"].strip(),
                    "bug_val": sd["bug_text"].strip(),
                })
            found_diff = True
            diff_line = diffs[0]["bug_line"]
            diff_var = diffs[0]["bug_var"]
            ref_val = diffs[0]["ref_val"]
            bug_val = diffs[0]["bug_val"]
        else:
            found_diff = False
            diff_line = diff_var = ref_val = bug_val = None
            diffs = []
    else:
        found_diff, diff_line, diff_var, ref_val, bug_val, diffs = compare_trace_logs(ref_log, buggy_log, role_mapping=role_mapping)
    # if found_diff and diff_line is not None:
    #     # if diff_var is not None and ref_val is not None:
    #     #     print(f"\nInserting asset on {diff_var} == {ref_val} at line {diff_line} in buggy file")
    #     #     buggy_with_assert_file = "sample_with_assert.c"
    #     #     buggy_with_assert_file = insert_assert_at_line(test_file, buggy_with_assert_file,line_no=diff_line,var_name=diff_var,ref_val_str=ref_val)

    #     #     print("\nRerunning with assert")
    #     #     traced_buggy_assert = "test_assert.traced.c"
    #     #     buggy_assert_exe = "test_assert_app"
    #     #     buggy_assert_log = None

    #     #     if instrument_c_code(buggy_with_assert_file, traced_buggy_assert):  # Create ref.traced.c
    #     #         if compile_c_code(traced_buggy_assert, buggy_assert_exe):  # Compile ref.traced.c
    #     #             stdout = run_c_executable(buggy_assert_exe)  # Runs code
    #     #             if stdout is not None:
    #     #                 print("\nCaptured Reference Output:\n" + stdout)
    #     #                 ref_swapped_log = parse_trace_log(stdout)  # Parse the log
    if diffs:
        print(f"\nAttempting individual swaps for {len(diffs)} differences")
        best_result = None  # Track the best (fewest remaining diffs) result across all attempts

        for idx, d in enumerate(diffs):
            line_for_swap = d["bug_line"] if d["bug_line"] is not None else d["ref_line"]
            if line_for_swap is None:
                continue
            func_label = d.get("function") or "unknown"
            print(f"\n--- Diff {idx+1}: function '{func_label}', line {line_for_swap}, var '{d['bug_var']}' ---")

            resolved = False
            for window in SWAP_WINDOW_LINES:
                ref_swapped_file = f"reference_swapped_{idx+1}.c"
                bug_swapped_file = f"sample_swapped_{idx+1}.c"
                swap_result = swap_code_region_between_files(
                    ref_file,
                    test_file,
                    center_line=line_for_swap,
                    window=window,
                    reference_out_path=ref_swapped_file,
                    buggy_out_path=bug_swapped_file
                )

                # Skip no-op swaps (regions were identical at this window size)
                if swap_result[0] is None:
                    continue

                ref_swapped_file, bug_swapped_file = swap_result
                print(f"\n RERUNNING ALGORITHM (diff {idx+1}, window={window})")
                print(f"\n Processing Swapped Reference File: {ref_swapped_file}")
                traced_ref_swapped = "ref_swapped.traced.c"
                ref_swapped_exe = "ref_swapped_app"
                ref_swapped_log = None

                if instrument_c_code(ref_swapped_file, traced_ref_swapped):
                    if compile_c_code(traced_ref_swapped, ref_swapped_exe):
                        stdout = run_c_executable(ref_swapped_exe)
                        if stdout is not None:
                            print("\nCaptured Reference Output:\n" + stdout)
                            ref_swapped_log = parse_trace_log(stdout)

                print(f"\nProcessing Swapped Buggy File: {bug_swapped_file}")
                traced_test_swapped = "test_swapped.traced.c"
                test_swapped_exe = "test_swapped_app"
                buggy_swapped_log = None

                if instrument_c_code(bug_swapped_file, traced_test_swapped):
                    if compile_c_code(traced_test_swapped, test_swapped_exe):
                        stdout = run_c_executable(test_swapped_exe)
                        if stdout is not None:
                            print("\nCaptured Buggy Output (up to crash):\n" + stdout)
                            buggy_swapped_log = parse_trace_log(stdout)

                # Guard: if either log wasn't generated (over-swap broke compilation/execution),
                # this window size is too large or caused invalid code — skip it
                if not ref_swapped_log or not buggy_swapped_log:
                    print(f"Window={window} produced invalid code (compilation/execution failed). Skipping.")
                    continue

                print("\nTRACE COMPARISON AFTER SWAP")
                found_after, _, _, _, _, remaining_diffs = compare_trace_logs(ref_swapped_log, buggy_swapped_log, role_mapping=role_mapping)

                if not found_after:
                    line = d["bug_line"] if d["bug_line"] is not None else d["ref_line"]
                    print(f"Resolved with window={window}. Main error in function '{func_label}' at line: {line}")
                    resolved = True
                    break
                else:
                    # Swap reduced or changed diffs — track the best result
                    remaining_count = len(remaining_diffs)
                    if best_result is None or remaining_count < best_result["remaining_count"]:
                        best_result = {
                            "diff_idx": idx,
                            "window": window,
                            "line": line_for_swap,
                            "remaining_count": remaining_count,
                        }

            if resolved:
                break
            else:
                print(f"Diff {idx+1} in function '{func_label}' at line {line_for_swap} could not be fully resolved with any window size.")

        if not resolved and best_result is not None:
            print(f"\nBest partial result: swapping line {best_result['line']} with window={best_result['window']} "
                  f"reduced differences to {best_result['remaining_count']}.")
    print("\nCleaning up .c and .traced.c files...")
    # Delete all the temporary files we created
    clean()
if __name__ == "__main__":
    import contextlib
    with open("trace_run_output.txt", "w") as f:
        with contextlib.redirect_stdout(f):

            main()