#include <stdio.h>

int math_stuff(int x)
{
    int a = x + 1;
    int b = x + 2;
    int c = x + 3;
    int d = x + 4;
    return a + b + c + d;
}

int main()
{
    int ans = math_stuff(10);
    printf("Result: %d\n", ans);
    return 0;
}