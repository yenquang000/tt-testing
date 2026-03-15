import sys
import os
import glob
import subprocess # Used to run external commands (like 'gcc' and the compiled C programs)
import itertools # Used for comparing the two trace logs (zip_longest)
import re  # Used for regular expressions, to parse the trace output from C
import json
import difflib
# Import the necessary components from the clang library
from clang.cindex import Index, Config, TranslationUnit, CursorKind, TypeKind


# This list tells the script where to look for the libclang library file, these are all the common places where they can be installed
possible_paths = [
   '/usr/lib/llvm-14/lib/libclang.so',
   '/usr/lib/llvm-13/lib/libclang.so',
   '/usr/lib/llvm-12/lib/libclang.so',
   '/usr/lib/x86_64-linux-gnu/libclang-14.so',
   '/Library/Developer/CommandLineTools/usr/lib/libclang.dylib',
   'C:/Program Files/LLVM/bin/libclang.dll', 
]


SWAP_WINDOW_LINES = 5


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
           inject_text = (
               f'    printf("TRACE:L{body_start_line}:Entering {func_name}\\n"); '
               f'fflush(stdout);\n'
               )


           for param in node.get_arguments():  # Loop through all parameters
               var_name = param.spelling
               var_type = get_variable_type(param)
               if var_type == 'pointer':
                   continue  #skip if pointer
               printf_format = get_printf_format(var_type)
               # Add trace print for the parameter
               inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'


           # Inject all this text after the line with the opening brace '{'
           add_injection(body_start_line, inject_text)


      # elif node.kind == CursorKind.VAR_DECL:
       #    var_name = node.spelling
        #   var_type = get_variable_type(node)
       #    if var_type == 'pointer': #don't trace pointers
         #      continue
          
           # Check if it has an initializer (e.g., '= 0')
          #     printf_format = get_printf_format(var_type)
           #    inject_text = f'    printf("TRACE:L{line}:{var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
           #    add_injection(line, inject_text)  # Inject *after* this line
       elif node.kind == CursorKind.FOR_STMT:
            # Find the loop variable (usually the first child if it's a declaration)
            for child in node.get_children():
                if child.kind == CursorKind.VAR_DECL:
                    var_name = child.spelling
                    var_type = get_variable_type(child)
                    # We inject this inside the loop body instead of after the for() line
                    try:
                        body = next(c for c in node.get_children() if c.kind == CursorKind.COMPOUND_STMT)
                        body_line = body.extent.start.line
                        printf_format = get_printf_format(var_type)
                        inject_text = f'    printf("TRACE:L{body_line}:(LoopVar){var_name}={printf_format}\\n", {var_name}); fflush(stdout);\n'
                        add_injection(body_line, inject_text)
                    except StopIteration:
                        pass # Loop has no {} body, harder to instrument safely

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


   with open(output_file, 'w') as f:
       f.write('#include <stdio.h>\n#include <assert.h>\n\n')


       for i, line_text in enumerate(source_lines):
           current_line_num = i + 1  # Line numbers are 1-based


           # Check for injections that go *before* this line (e.g., return)
           if current_line_num in injections and any("Returning" in inj for inj in injections[current_line_num]):
               for injection in injections[current_line_num]:
                   if "Returning" in injection:
                       # Get the indentation of the original line
                       indentation = len(line_text) - len(line_text.lstrip(' '))
                       f.write(' ' * indentation + injection)  # Write injection with same indent


           # Write the original line itself
           f.write(line_text)


           # Check for injections that go *after* this line (e.g., assign, func entry)
           if current_line_num in injections and not any("Returning" in inj for inj in injections[current_line_num]):
               for injection in injections[current_line_num]:
                   indentation = len(line_text) - len(line_text.lstrip(' '))
                   if '{' in line_text:  # If it's a function entry line
                       indentation += 4  # Add 4 spaces for body indentation
                   f.write(' ' * indentation + injection)  # Write injection


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
            f.writelines(lines)
        result = compile_c_code(tmp_path, "swap_test_exe")
        return result
    except Exception:
        return False
    finally:
        # clean up temp files
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        if os.path.exists("swap_test_exe"):
            os.remove("swap_test_exe")
        if os.path.exists("swap_test_exe.exe"):
            os.remove("swap_test_exe.exe")


def run_c_executable(exe_file):
   """Runs a compiled C executable and returns its captured stdout."""
   #'./exe_file' on Linux/macOS and '.\exe_file' on Windows
   run_command = f"./{exe_file}" if os.name != 'nt' else f".\\{exe_file}"


   try:
       
       result = subprocess.run(
           run_command, check=True, capture_output=True, text=True, shell=True)
       return result.stdout  # return the text output from the C program
   except subprocess.CalledProcessError as e:
       #catch crashes
       print(f"Execution failed for {exe_file}:")
       print(e.stderr)  
       return e.stdout
   finally:     
       exe_path = exe_file
       if os.name == 'nt' and not exe_file.endswith('.exe'):
           exe_path = f"{exe_file}.exe" 
       if os.path.exists(exe_path):
           os.remove(exe_path) 




def parse_trace_log(stdout):
   """Parses 'TRACE:var=val' lines from the C program's stdout."""
   log = []  # Start with an empty log
   # Define a regular expression to find lines starting with "TRACE:"
   pattern_with_val = re.compile(r'^TRACE:L(\d+):(.*?)=(.*)$')
   pattern_noval = re.compile(r'^TRACE:L(\d+):(.*)$')
   for line in stdout.splitlines():  # Loop over each line of output
       match = pattern_with_val.match(line)  # See if the line matches our regex
       if match:
           # If it matches, "groups()" gives us the two captured parts
           lineno = int(match.group(1))
           label = match.group(2).strip()
           val = match.group(3).strip()
           log.append((lineno, label, val))  # Add (variable, value) tuple to log
       else:
           match2 = pattern_noval.match(line)
           if match2:
               lineno = int(match2.group(1))
               label = match2.group(2).strip()
               log.append((lineno, label, None))
   return log


def compare_trace_logs(ref_log, buggy_log):
   print("\n TRACE COMPARISON")
   print("Comparing logs to find the first point of divergence...\n")


   if not ref_log or not buggy_log:
       print("Error: Could not generate one or both trace logs. Exiting.")
       return False, None, None, None, None, []
   diffs = []
   first_diff = None
   # zip_longest compares two lists, pairing items.
   # If one list is shorter, it fills with `fillvalue`.
   for i, (ref_entry, buggy_entry) in enumerate(itertools.zip_longest(ref_log, buggy_log, fillvalue=(None, "(Missing)", "(Missing)"))):


       ref_line, ref_var, ref_val = ref_entry
       bug_line, bug_var, bug_val = buggy_entry


       # If the entries don't match, and we haven't found a diff yet
       if ref_entry != buggy_entry and ref_val is not None and bug_val is not None and ref_val != bug_val:
           diff_info = {
               "trace_index": i,
               "ref_line": ref_line,
               "bug_line": bug_line,
               "ref_var": ref_var,
               "bug_var": bug_var,
               "ref_val": ref_val,
               "bug_val": bug_val
           }
           diffs.append(diff_info)


   # This handles the case where the buggy code crashed
   if not diffs and len(ref_log) > len(buggy_log):
       print("DIVERGENCE FOUND!")
       print("Buggy code crashed or stopped early.")
       idx = len(buggy_log)
       ref_line, ref_var, ref_val = ref_log[idx]
       
       diff_info = {
           "trace_index": idx,
           "ref_line": ref_line,
           "bug_line": None,
           "ref_var": ref_var,
           "bug_var": None,
           "ref_val": ref_val,
           "bug_val": None
       }
       diffs.append(diff_info)
   elif not diffs:  
       print("No differences found in trace logs. The logic appears identical.")
       return False, None, None, None, None, []
   try:
       with open("trace_differences.json", "w") as f:
           json.dump(diffs, f, indent = 2)
   except Exception as e:
       print(f"Could not savve to JSON File; {e}")
   first_diff = diffs[0]
   first_line = first_diff["bug_line"] if first_diff["bug_line"] is not None else first_diff["ref_line"] 
   first_var = first_diff["bug_var"] if first_diff["bug_var"] is not None else first_diff["ref_var"]
   first_ref_var = first_diff["ref_val"]
   first_bug_val = first_diff["bug_val"]


   return True, first_line, first_var, first_ref_var, first_bug_val, diffs
def swap_code_region_between_files(
       reference_path, buggy_path, center_line,
       window=SWAP_WINDOW_LINES,
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
      
       with open(reference_out_path, "w") as f:
           f.writelines(ref_lines)
       with open(buggy_out_path, "w") as f:
           f.writelines(bug_lines)
       return reference_out_path, buggy_out_path
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


   # Process Reference File
   print(f" Processing Reference File: {ref_file} ")
   traced_ref_file = "ref.traced.c"  # The new file we will create
   ref_exe = "ref_app"  # The compiled executable we will create
   ref_log = None
   if instrument_c_code(ref_file, traced_ref_file):  # Create ref.traced.c
       if compile_c_code(traced_ref_file, ref_exe):  # Compile ref.traced.c
           stdout = run_c_executable(ref_exe)  # Runs code
           if stdout is not None:
               print("\nCaptured Reference Output:\n" + stdout)
               ref_log = parse_trace_log(stdout)  # Parse the log


   # Process Buggy File
   print(f"\nProcessing Buggy File: {test_file}")
   traced_test_file = "test.traced.c"
   test_exe = "test_app"
   buggy_log = None
   if instrument_c_code(test_file, traced_test_file): 
       if compile_c_code(traced_test_file, test_exe):
           stdout = run_c_executable(test_exe)  # (This will crash due to assert)
           if stdout is not None:
               # This will print the partial log captured before the crash
               print("\nCaptured Buggy Output (up to crash):\n" + stdout)
               buggy_log = parse_trace_log(stdout)


   found_diff, diff_line, diff_var, ref_val, bug_val, diffs = compare_trace_logs(ref_log, buggy_log)
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
   #     print(f"\nAttempting swap around source line {diff_line}")
   #     ref_swapped_file, bug_swapped_file = swap_code_region_between_files(
   #         ref_file, test_file, center_line = diff_line, window = SWAP_WINDOW_LINES,
   #         reference_out_path="reference_swapped.c", buggy_out_path = "sample_swapped.c"
   #     )
   if diffs:
       print(f"Attempting individual swaps for {len(diffs)} differneces")
       for idx, d in enumerate(diffs):
           line_for_swap = d["bug_line"] if d["bug_line"] is not None else d["ref_line"]
           if line_for_swap is None:
               continue
           ref_swapped_file = f"reference_swapped_{idx+1}.c"
           bug_swapped_file = f"sample_swapped_{idx+1}.c"
           ref_swapped_file, bug_swapped_file = swap_code_region_between_files(
               ref_file,
               test_file,
               center_line = line_for_swap,
               #window = SWAP_WINDOW_LINES,
               reference_out_path = ref_swapped_file,
               buggy_out_path = bug_swapped_file
           )
           print("\n RERUNNING ALGORITHM")
           print(f"\n Processing Swapped Reference File : {ref_swapped_file}")
           traced_ref_swapped = "ref_swapped.traced.c"
           ref_swapped_exe = "ref_swapped_app"
           ref_swapped_log = None


           if instrument_c_code(ref_swapped_file, traced_ref_swapped):  # Create ref.traced.c
               if compile_c_code(traced_ref_swapped, ref_swapped_exe):  # Compile ref.traced.c
                   stdout = run_c_executable(ref_swapped_exe)  # Runs code
                   if stdout is not None:
                       print("\nCaptured Reference Output:\n" + stdout)
                       ref_swapped_log = parse_trace_log(stdout)  # Parse the log
           print(f"\nProcessing Buggy File: {test_file}")
           traced_test_swapped = "test_swapped.traced.c"
           test_swapped_exe = "test_swapped_app"
           buggy_swapped_log = None


           if instrument_c_code(bug_swapped_file, traced_test_swapped): 
               if compile_c_code(traced_test_swapped, test_swapped_exe):
                   stdout = run_c_executable(test_swapped_exe)  # (This will crash due to assert)
                   if stdout is not None:
                       # This will print the partial log captured before the crash
                       print("\nCaptured Buggy Output (up to crash):\n" + stdout)
                       buggy_swapped_log = parse_trace_log(stdout)
           print("\nTRACE COMPARISON AFTER SWAP")
           found_after, _, _, _, _,_ = compare_trace_logs(ref_swapped_log, buggy_swapped_log)
           if not found_after:
               diff = diffs[idx]
               line = diff["bug_line"] if diff["bug_line"] is not None else diff["ref_line"] 
               print(f"No differences, after swap, this is the main error at line: {line}")
               break
   print("\nCleaning up .c and .traced.c files...")
   # Delete all the temporary files we created
   clean()
if __name__ == "__main__":
   import contextlib
   with open("trace_run_output.txt", "w") as f:
       with contextlib.redirect_stdout(f):
           main()

