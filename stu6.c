#include <stdio.h>
#include <assert.h>

float calculate_average(int arr[], int size)
{
    int total = 0;
    for (int i = 0; i < size; i++)
    {
        total += arr[i];
    }
    // BUG: assert will fail and crash the program
    assert(total == 999);
    float avg = (float)total / size;
    return avg;
}

int main()
{
    int scores[5] = {10, 20, 30, 40, 50};
    float result = calculate_average(scores, 5);
    printf("Average: %.2f\n", result);
    return 0;
}