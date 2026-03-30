int compute_magic_number(int a, int b)
{
    int sum = a + b;
    int diff = a + b; // BUG: Should be subtraction (a - b)
    int result = sum * diff;
    return result;
}

int main()
{
    int ans = compute_magic_number(10, 4);
    printf("Answer: %d\n", ans);
    return 0;
}