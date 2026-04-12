#include <stdio.h>

float calculate_average(int arr[], int size)
{
    // BUG: also adds 10 to total
    float avg = 0;
    int total = 0;
    for (int i = 0; i < size; i++)
    {
        total += arr[i];
    }
    total += 10;
    avg = (float)total / size;
    return avg;
}

int main()
{
    int scores[5] = {10, 20, 30, 40, 50};
    float result = calculate_average(scores, 5);
    printf("Average: %.2f\n", result);
    return 0;
}