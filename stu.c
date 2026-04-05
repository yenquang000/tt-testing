#include <stdio.h>

int math_stuff(int x)
{

    int c = x + 3;
    int d = x + 4;
    int a = x + 1;
    int b = x + 2;
    // BUG: Added + 1
    return a + b + c + d + 1;
}

int main()
{
    int ans = math_stuff(10);
    printf("Result: %d\n", ans);
    return 0;
}