#include <stdio.h>

float calculate_average(int arr[], int size)
{
    int total = 0;
    for (int i = 0; i < size; i++)
    {
        total += arr[i];
    }
    // BUG 1: adds 1 to total
    total += 1;
    float avg = (float)total / size;
    return avg;
}

int main()
{
    // BUG 2: wrong array values
    int scores[5] = {10, 20, 30, 40, 100};
    float result = calculate_average(scores, 5);
    printf("Average: %.2f\n", result);
    return 0;
}